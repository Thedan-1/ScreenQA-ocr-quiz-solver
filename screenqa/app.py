# -*- coding: utf-8 -*-
"""ScreenQA —— 框一块屏幕区域，内容一变就自动截图、识别、给答案。

用法：python main.py
框选之后就不用再碰这个程序了，全部靠全局热键控制：
    F6  显示/隐藏识别原文     F7  显示/隐藏面板     F8  暂停/继续
    F9  立即重新识别          F10 重新框选区域      F11 显示/隐藏取景框
    F12 退出
"""

import ctypes
import json
import os
import queue
import sys
import time
import tkinter as tk

try:
    from mss import MSS as _MSS
except ImportError:
    from mss import mss as _MSS

from . import ui
from .capture import RegionWatcher
from .solver import NO_QUESTION, Solver

# 配置放在项目根目录，不塞进包里
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
EXAMPLE_PATH = os.path.join(ROOT, "config.example.json")

DEFAULTS = {
    # text = 本地 OCR + 纯语言模型（DeepSeek 等）；vision = 截图直接给多模态模型
    "backend": "text",
    "api": {
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        # 没在上面填 key 时，只从这一个环境变量里取。
        # 故意不去捡 OPENAI_API_KEY 之类：那会把某一家的密钥发给另一家的服务器。
        # 换服务商时，请连着 base_url、model 一起改这个名字。
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
        "temperature": 0,
        "max_tokens": 1000,
    },
    "vision": {"model": "claude-sonnet-5", "api_key": ""},
    "ocr": {"min_confidence": 0.5, "upscale": 2},

    "region": None,
    "panel_geometry": "460x560+40+40",
    "panel_alpha": 0.97,
    "show_region_outline": True,   # 常驻取景框，截图时会闪绿
    "show_ocr_text": True,         # 面板底部显示识别到的原文

    "poll_interval": 0.25,
    "diff_threshold": 0.012,
    "stable_frames": 2,
    "min_gap": 0.8,

    "hotkeys": {
        "toggle_ocr": "f6",
        "toggle_panel": "f7",
        "toggle_pause": "f8",
        "redo": "f9",
        "reselect": "f10",
        "toggle_outline": "f11",
        "quit": "f12",
    },
}


def _merge(base, override):
    """按层合并配置，这样旧的 config.json 少了新字段也能跑。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


LOAD_ERROR = None


def load_config():
    """读 config.json。用 utf-8-sig 是因为记事本等编辑器会写入 BOM，
    普通 utf-8 解码会在第一个字符就报错，整份配置被丢掉。"""
    global LOAD_ERROR
    cfg = dict(DEFAULTS)
    # 刚 clone 下来只有 config.example.json，复制一份出来给用户改
    if not os.path.exists(CONFIG_PATH) and os.path.exists(EXAMPLE_PATH):
        try:
            import shutil
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
            print("已根据 config.example.json 生成 config.json，请填入 API key。")
        except Exception as exc:
            print("生成 config.json 失败：%s" % exc)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg = _merge(DEFAULTS, json.load(f))
        except Exception as exc:
            LOAD_ERROR = u"config.json 读取失败（%s: %s），本次用默认设置。" % (
                type(exc).__name__, exc)
            print(LOAD_ERROR)
    # 防呆：两个字段挨着，容易把密钥本身填进 api_key_env
    env_name = cfg["api"].get("api_key_env") or "DEEPSEEK_API_KEY"
    if env_name.startswith("sk-") or " " in env_name:
        if not cfg["api"].get("api_key"):
            cfg["api"]["api_key"] = env_name
        env_name = "DEEPSEEK_API_KEY"
        cfg["api"]["api_key_env"] = env_name
        print("提示：api_key_env 里像是密钥本身，已当作 api_key 使用。")

    # 没在 config 里填 key 就查指定的那一个环境变量
    if not cfg["api"].get("api_key"):
        cfg["api"]["api_key"] = os.environ.get(env_name, "")
    return cfg


def save_config(cfg):
    try:
        out = json.loads(json.dumps(cfg))
        # 环境变量来的 key 不要偷偷写进文件
        if out.get("api", {}).get("api_key") and not _key_in_file():
            out["api"]["api_key"] = ""
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print("配置保存失败：%s" % exc)


def _key_in_file():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return bool(json.load(f).get("api", {}).get("api_key"))
    except Exception:
        return False


def enable_dpi_awareness():
    """让 tkinter 的坐标和 mss 的物理像素对齐，否则高 DPI 下框选会偏。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class App(object):
    def __init__(self):
        self.cfg = load_config()
        self.root = tk.Tk()
        self.root.withdraw()

        with _MSS() as sct:
            self.bounds = dict(sct.monitors[0])
            self.monitors = [dict(m) for m in sct.monitors[1:]]

        self.events = queue.Queue()
        self.panel = None
        self.indicator = None
        self.watcher = None
        self.solver = None
        self.count = 0
        self.last_shot = None
        self.gate_buf = ""
        self.gate_open = False
        self.hotkeys_ok = False

    # ---------- 事件（后台线程 -> 主线程） ----------
    def post(self, kind, *args):
        self.events.put((kind, args))

    def pump(self):
        while True:
            try:
                kind, args = self.events.get_nowait()
            except queue.Empty:
                break
            handler = getattr(self, "_on_" + kind, None)
            if handler:
                try:
                    handler(*args)
                except Exception as exc:
                    print("事件 %s 处理出错：%s" % (kind, exc))
        self.root.after(60, self.pump)

    # ---------- 启动 ----------
    def start(self):
        enable_dpi_awareness()

        region = self.cfg.get("region")
        if not region:
            region = self.ask_region()
            if not region:
                print("没有框选区域，退出。")
                return

        self.panel = ui.AnswerPanel(self.root, self.cfg["panel_geometry"],
                                    self.cfg["panel_alpha"],
                                    self.cfg["show_ocr_text"])
        if self.cfg["show_region_outline"]:
            self.indicator = ui.RegionIndicator(self.root, region)

        self.panel.set_stage(u"● 启动中…", ui.DIM)
        self.panel.set_meta(u"尚未截图")
        self.panel.set_text(
            u"监视区域：%d×%d\n\n"
            u"屏幕上那圈细线就是监视范围。\n"
            u"每次真的截图，它会闪一下绿色。\n\n"
            u"翻到题目后会自动识别，不用再操作本程序。"
            % (region["width"], region["height"]))

        if LOAD_ERROR:
            self.panel.set_stage(u"● 配置没读进来", ui.YELLOW)
            self.panel.set_text(LOAD_ERROR + u"\n\n把 config.json 另存为"
                                u"「UTF-8（无 BOM）」再重启即可。", "err")

        backend = self.cfg.get("backend", "text")
        if backend == "text" and not self.cfg["api"].get("api_key"):
            self.panel.set_stage(u"● 还没填 API key", ui.YELLOW)
            self.panel.set_text(
                u"OCR 可以正常跑，但没法提问。\n\n"
                u"打开 config.json，把 api.api_key 填上：\n"
                u'  "api_key": "sk-你的deepseek密钥"\n\n'
                u"或者设置环境变量 DEEPSEEK_API_KEY 后重启。\n\n"
                u"（现在启动也行，你可以先看看下面识别出的文字准不准。）",
                "err")

        self.solver = Solver(
            self.cfg,
            on_stage=lambda g, s, d: self.post("stage", g, s, d),
            on_ocr=lambda g, r: self.post("ocr", g, r),
            on_delta=lambda g, t: self.post("delta", g, t),
            on_done=lambda g, t, d: self.post("done", g, t, d),
            on_error=lambda g, m: self.post("error", g, m),
        )
        self.solver.start()

        self.watcher = RegionWatcher(
            region,
            on_settled=lambda img: self.post("settled", img),
            on_state=lambda s: None,
            poll_interval=self.cfg["poll_interval"],
            diff_threshold=self.cfg["diff_threshold"],
            stable_frames=self.cfg["stable_frames"],
            min_gap=self.cfg["min_gap"],
        )
        self.watcher.start()

        self.bind_hotkeys()
        self.root.after(60, self.pump)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.mainloop()

    def ask_region(self):
        region = ui.select_region(self.root, self.bounds, self.monitors)
        if region:
            self.cfg["region"] = region
            save_config(self.cfg)
        return region

    # ---------- 热键 ----------
    def bind_hotkeys(self):
        hk = self.cfg["hotkeys"]
        actions = {
            hk["toggle_ocr"]: lambda: self.post("toggle_ocr"),
            hk["toggle_panel"]: lambda: self.post("toggle_panel"),
            hk["toggle_pause"]: lambda: self.post("toggle_pause"),
            hk["redo"]: lambda: self.post("redo"),
            hk["reselect"]: lambda: self.post("reselect"),
            hk["toggle_outline"]: lambda: self.post("toggle_outline"),
            hk["quit"]: lambda: self.post("quit"),
        }
        try:
            import keyboard
            for key, fn in actions.items():
                keyboard.add_hotkey(key, fn, suppress=False)
            self.hotkeys_ok = True
        except Exception as exc:
            print("全局热键不可用（%s），改为面板内按键。" % exc)
            self.panel.win.focus_force()
            for key, fn in actions.items():
                self.panel.win.bind("<%s>" % key.upper(), lambda e, f=fn: f())

    # ---------- 截图 ----------
    def _on_settled(self, img):
        self.count += 1
        self.last_shot = time.time()
        self.gate_buf = ""
        self.gate_open = False

        if self.indicator:
            self.indicator.flash()          # 屏幕上闪一下，明确告诉你截了
        w, h = img.size
        self.panel.set_counter(u"#%d" % self.count)
        self.panel.set_stage(u"● 已截图", ui.GREEN)
        self.panel.set_meta(u"最近截图 %s · %d×%d · 本次共 %d 张"
                            % (time.strftime("%H:%M:%S"), w, h, self.count),
                            ui.GREEN)
        self.panel.clear()
        self.solver.submit(img)

    # ---------- 后端阶段 ----------
    def _on_stage(self, gen, stage, detail):
        colors = {"loading": ui.DIM, "ready": ui.DIM,
                  "ocr": ui.YELLOW, "ask": ui.ACCENT}
        if stage == "ready":
            self.panel.set_stage(u"● %s，监听中" % detail, ui.DIM)
        else:
            self.panel.set_stage(u"● %s" % detail, colors.get(stage, ui.ACCENT))

    def _on_ocr(self, gen, res):
        if res.get("error"):
            self.panel.set_ocr(res["error"], u"识别失败")
            return
        self.panel.set_ocr(res["text"] or u"(这一帧没识别到文字)",
                           u"识别到的文字 · %d 字 · 置信度 %.2f · %.1fs"
                           % (res["n_chars"], res["confidence"], res["elapsed"]))
        self.panel.set_stage(u"● 已识别 %d 字，提问中…" % res["n_chars"],
                             ui.ACCENT)

    # ---------- 模型输出 ----------
    def _on_delta(self, gen, chunk):
        if self.gate_open:
            self.panel.append(chunk)
            return
        # 先攒够能判断 NO_QUESTION 的长度，避免哨兵值闪现在屏幕上
        self.gate_buf += chunk
        stripped = self.gate_buf.strip()
        if stripped.startswith(NO_QUESTION):
            return
        if len(stripped) >= len(NO_QUESTION) or "\n" in self.gate_buf:
            self.gate_open = True
            self.panel.append(self.gate_buf)

    def _on_done(self, gen, text, elapsed):
        if text.strip().startswith(NO_QUESTION) or not text.strip():
            self.count -= 1
            self.panel.set_counter(u"#%d" % max(self.count, 0))
            self.panel.set_stage(u"● 没看到题目，继续等", ui.DIM)
            self.panel.set_text(u"这一帧里没有可作答的内容。")
            return
        self.panel.render_answer(text)
        self.panel.set_stage(u"● 完成  %.1fs   %s"
                             % (elapsed, time.strftime("%H:%M:%S")), ui.GREEN)

    def _on_error(self, gen, msg):
        self.panel.set_stage(u"● 出错", ui.RED)
        if not self.gate_open and self.gate_buf:
            self.panel.append(self.gate_buf)
            self.gate_open = True
        self.panel.append(u"\n\n[失败] %s" % msg, "err")

    # ---------- 热键动作 ----------
    def _on_toggle_ocr(self):
        on = self.panel.toggle_ocr()
        self.cfg["show_ocr_text"] = on

    def _on_toggle_panel(self):
        self.panel.toggle()

    def _on_toggle_outline(self):
        if self.indicator:
            on = self.indicator.toggle()
            self.cfg["show_region_outline"] = on
        else:
            self.indicator = ui.RegionIndicator(self.root, self.watcher.region)
            self.cfg["show_region_outline"] = True

    def _on_toggle_pause(self):
        self.watcher.paused = not self.watcher.paused
        if self.watcher.paused:
            self.panel.set_stage(u"● 已暂停（F8 继续）", ui.YELLOW)
            self.panel.set_meta(u"已暂停，不会再截图", ui.YELLOW)
            if self.indicator:
                self.indicator.paused()
        else:
            self.panel.set_stage(u"● 已恢复监听", ui.ACCENT)
            self.panel.set_meta(u"监听中 · 本次共截图 %d 张" % self.count)
            if self.indicator:
                self.indicator.idle()

    def _on_redo(self):
        self.watcher.force()

    def _on_reselect(self):
        self.watcher.paused = True
        region = ui.select_region(self.root, self.bounds, self.monitors)
        if region:
            self.cfg["region"] = region
            save_config(self.cfg)
            self.watcher.set_region(region)
            if self.indicator:
                self.indicator.set_region(region)
            self.panel.set_stage(u"● 新区域 %d×%d"
                                 % (region["width"], region["height"]), ui.ACCENT)
        self.watcher.paused = False

    def _on_quit(self):
        self.quit()

    def quit(self):
        try:
            self.cfg["panel_geometry"] = self.panel.geometry()
            save_config(self.cfg)
        except Exception:
            pass
        if self.watcher:
            self.watcher.stop()
        if self.solver:
            self.solver.stop()
        if self.indicator:
            self.indicator.destroy()
        if self.hotkeys_ok:
            try:
                import keyboard
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        self.root.quit()
        self.root.destroy()


def main():
    App().start()


if __name__ == "__main__":
    sys.exit(main())

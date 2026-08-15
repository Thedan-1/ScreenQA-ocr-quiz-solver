# -*- coding: utf-8 -*-
"""框选遮罩 + 取景框 + 答案面板（tkinter，无额外依赖）。"""

import tkinter as tk

FONT_UI = ("Microsoft YaHei UI", 9)
FONT_SMALL = ("Microsoft YaHei UI", 8)
FONT_BODY = ("Microsoft YaHei UI", 11)

BG = "#14161a"
BAR = "#1d2026"
FG = "#e8eaed"
DIM = "#8b929c"
ACCENT = "#7cc4ff"
GREEN = "#4ade80"
YELLOW = "#f5c451"
RED = "#ff7b72"

# 取景框内部要抠掉的颜色，Windows 上这块既透明也不挡鼠标
_TRANSPARENT = "#ff00fe"


def select_region(root, bounds, monitors=None):
    """全屏半透明遮罩，拖一个框出来。

    bounds 是虚拟桌面范围（多屏时是所有屏幕的并集），monitors 是各个屏幕，
    只用来把提示文字画在每块屏的中间，而不是落在两屏拼缝上。
    返回 {'left','top','width','height'}，按 Esc 取消返回 None。
    """
    result = {}

    ov = tk.Toplevel(root)
    ov.overrideredirect(True)
    ov.geometry("%dx%d+%d+%d" % (bounds["width"], bounds["height"],
                                 bounds["left"], bounds["top"]))
    ov.attributes("-topmost", True)
    ov.attributes("-alpha", 0.35)
    ov.configure(bg="black")
    ov.config(cursor="crosshair")

    cv = tk.Canvas(ov, bg="black", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    for mon in (monitors or [bounds]):
        cx = mon["left"] - bounds["left"] + mon["width"] // 2
        cy = mon["top"] - bounds["top"] + 60
        cv.create_text(cx, cy, text=u"拖动鼠标框选题目区域    ·    Esc 取消",
                       fill="white", font=("Microsoft YaHei UI", 16))

    state = {"x": 0, "y": 0, "rect": None, "label": None}

    def on_press(e):
        state["x"], state["y"] = e.x, e.y
        if state["rect"]:
            cv.delete(state["rect"])
        state["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y,
                                            outline=ACCENT, width=2)

    def on_drag(e):
        if not state["rect"]:
            return
        cv.coords(state["rect"], state["x"], state["y"], e.x, e.y)
        txt = u"%d × %d" % (abs(e.x - state["x"]), abs(e.y - state["y"]))
        if state["label"]:
            cv.delete(state["label"])
        state["label"] = cv.create_text(e.x + 44, e.y + 14, text=txt,
                                        fill=ACCENT, font=FONT_UI)

    def on_release(e):
        x1, y1 = min(state["x"], e.x), min(state["y"], e.y)
        x2, y2 = max(state["x"], e.x), max(state["y"], e.y)
        if x2 - x1 >= 20 and y2 - y1 >= 20:
            result.update({
                "left": bounds["left"] + x1,
                "top": bounds["top"] + y1,
                "width": x2 - x1,
                "height": y2 - y1,
            })
        ov.destroy()

    ov.bind("<ButtonPress-1>", on_press)
    ov.bind("<B1-Motion>", on_drag)
    ov.bind("<ButtonRelease-1>", on_release)
    ov.bind("<Escape>", lambda e: ov.destroy())
    ov.focus_force()
    root.wait_window(ov)
    return result or None


class RegionIndicator(object):
    """罩在监视区域上的取景框。

    平时是一圈暗色细线，告诉你"程序在盯这块"；每次真的截图时闪一下绿色，
    所以截没截图、什么时候截的，一眼就能看见。中间是透明的，不挡鼠标。
    """

    BORDER = 2

    def __init__(self, root, region):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", _TRANSPARENT)
        except tk.TclError:
            pass  # 非 Windows 平台就是个实心框，功能不受影响
        self.win.configure(bg=_TRANSPARENT)

        self.cv = tk.Canvas(self.win, bg=_TRANSPARENT, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.rect = None
        self._flash_job = None
        self._visible = True
        self.set_region(region)

    def set_region(self, region):
        self.region = dict(region)
        b = self.BORDER
        self.win.geometry("%dx%d+%d+%d" % (
            region["width"] + b * 2, region["height"] + b * 2,
            region["left"] - b, region["top"] - b))
        self.cv.delete("all")
        self.rect = self.cv.create_rectangle(
            b // 2 + 1, b // 2 + 1,
            region["width"] + b + b // 2, region["height"] + b + b // 2,
            outline="#3d4a5c", width=b)

    def _set(self, color, width):
        try:
            self.cv.itemconfigure(self.rect, outline=color, width=width)
        except tk.TclError:
            pass

    def idle(self):
        self._set("#3d4a5c", self.BORDER)

    def paused(self):
        self._set(YELLOW, self.BORDER)

    def flash(self, ms=450):
        """截图的一瞬间闪一下。"""
        self._set(GREEN, 3)
        if self._flash_job:
            try:
                self.root.after_cancel(self._flash_job)
            except Exception:
                pass
        self._flash_job = self.root.after(ms, self.idle)

    def toggle(self):
        self._visible = not self._visible
        if self._visible:
            self.win.deiconify()
            self.win.attributes("-topmost", True)
        else:
            self.win.withdraw()
        return self._visible

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class AnswerPanel(object):
    """置顶的无边框答案窗口。按住顶部可以拖动，其余时间不用碰它。"""

    SECTIONS = ((u"【题目】", "sec_q"), (u"【答案】", "sec_a"), (u"【解析】", "sec_x"))

    def __init__(self, root, geometry="460x560+40+40", alpha=0.97,
                 show_ocr=True):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", alpha)
        self.win.geometry(geometry)
        self.win.configure(bg=BG)

        # --- 顶部状态条 ---
        head = tk.Frame(self.win, bg=BAR, height=26)
        head.pack(side="top", fill="x")
        head.pack_propagate(False)
        self.stage = tk.Label(head, text=u"● 启动中", bg=BAR, fg=DIM,
                              font=FONT_UI, anchor="w", padx=8)
        self.stage.pack(side="left", fill="both", expand=True)
        self.counter = tk.Label(head, text="", bg=BAR, fg=DIM,
                                font=FONT_UI, padx=8)
        self.counter.pack(side="right")

        # --- 截图流水（第二行，常驻，让人知道到底截没截） ---
        self.meta = tk.Label(self.win, text=u"尚未截图", bg="#191c22", fg=DIM,
                             font=FONT_SMALL, anchor="w", padx=9, pady=3)
        self.meta.pack(side="top", fill="x")

        # --- 底部热键条（必须先 pack，否则被正文挤没） ---
        foot = tk.Label(self.win,
                        text=u"F6 原文 · F7 面板 · F8 暂停 · F9 重答 · "
                             u"F10 框选 · F11 取景框 · F12 退出",
                        bg=BAR, fg=DIM, font=("Microsoft YaHei UI", 8),
                        anchor="w", padx=8, pady=4)
        foot.pack(side="bottom", fill="x")

        # --- OCR 原文预览 ---
        self.ocr_box = tk.Frame(self.win, bg=BG)
        self.ocr_title = tk.Label(self.ocr_box, text=u"识别到的文字",
                                  bg="#191c22", fg=DIM, font=FONT_SMALL,
                                  anchor="w", padx=9, pady=3)
        self.ocr_title.pack(side="top", fill="x")
        self.ocr_text = tk.Text(self.ocr_box, bg="#101216", fg="#7f8790",
                                font=FONT_SMALL, width=1, height=5,
                                wrap="word", bd=0, padx=9, pady=6,
                                insertwidth=0, highlightthickness=0)
        self.ocr_text.pack(side="top", fill="both")
        self.ocr_text.configure(state="disabled")
        self._show_ocr = show_ocr
        if show_ocr:
            self.ocr_box.pack(side="bottom", fill="x")

        # --- 正文 ---
        self.text = tk.Text(self.win, bg=BG, fg=FG, font=FONT_BODY,
                            width=1, height=1, wrap="word", bd=0,
                            padx=12, pady=10, insertwidth=0,
                            spacing1=2, spacing3=4, highlightthickness=0)
        self.text.pack(side="top", fill="both", expand=True)
        self.text.tag_configure("sec_q", foreground="#b9c0cb")
        self.text.tag_configure("sec_a", foreground=GREEN,
                                font=("Microsoft YaHei UI", 12, "bold"))
        self.text.tag_configure("sec_x", foreground="#98a0ab")
        self.text.tag_configure("body", foreground=FG)
        self.text.tag_configure("err", foreground=RED)
        self.text.configure(state="disabled")

        for w in (head, self.stage, self.counter):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        self._drag = (0, 0)
        self._visible = True

    # --- 拖动 ---
    def _drag_start(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag_move(self, e):
        self.win.geometry("+%d+%d" % (e.x_root - self._drag[0],
                                      e.y_root - self._drag[1]))

    # --- 状态 ---
    def set_stage(self, text, color=ACCENT):
        self.stage.configure(text=text, fg=color)

    def set_meta(self, text, color=DIM):
        self.meta.configure(text=text, fg=color)

    def set_counter(self, text):
        self.counter.configure(text=text)

    # --- 正文 ---
    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def append(self, chunk, tag="body"):
        self.text.configure(state="normal")
        self.text.insert("end", chunk, tag)
        self.text.see("end")
        self.text.configure(state="disabled")

    def set_text(self, content, tag="body"):
        self.clear()
        self.append(content, tag)

    def render_answer(self, content):
        """按【题目】【答案】【解析】分段上色，流式结束后调用。"""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        cur = "body"
        for line in content.splitlines(True):
            stripped = line.lstrip()
            for marker, tag in self.SECTIONS:
                if stripped.startswith(marker):
                    cur = tag
                    break
            self.text.insert("end", line, cur)
        self.text.see("1.0")
        self.text.configure(state="disabled")

    # --- OCR 原文 ---
    def set_ocr(self, text, title=None):
        self.ocr_title.configure(text=title or u"识别到的文字")
        self.ocr_text.configure(state="normal")
        self.ocr_text.delete("1.0", "end")
        self.ocr_text.insert("end", text or u"(空)")
        self.ocr_text.configure(state="disabled")

    def toggle_ocr(self):
        self._show_ocr = not self._show_ocr
        if self._show_ocr:
            self.ocr_box.pack(side="bottom", fill="x")
        else:
            self.ocr_box.pack_forget()
        return self._show_ocr

    # --- 窗口 ---
    def toggle(self):
        self._visible = not self._visible
        if self._visible:
            self.win.deiconify()
            self.win.attributes("-topmost", True)
        else:
            self.win.withdraw()

    def geometry(self):
        self.win.update_idletasks()
        return "%dx%d+%d+%d" % (self.win.winfo_width(), self.win.winfo_height(),
                                self.win.winfo_x(), self.win.winfo_y())

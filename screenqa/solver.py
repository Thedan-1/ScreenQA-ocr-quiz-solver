# -*- coding: utf-8 -*-
"""答题后端。

两条路：
  text   —— 本地 OCR 出文字，再发给任意 OpenAI 兼容的纯语言模型（DeepSeek、
            Kimi、通义、本地 ollama…… 改 base_url 和 model 就行）
  vision —— 直接把截图发给 Claude 多模态模型，不经过 OCR

只保留“最新一题”：上一题还没答完用户就翻页了，旧结果会被丢弃。
"""

import base64
import io
import threading
import time

from . import ocr

# 用户要求的固定输出格式，模型只许回这三段
SYSTEM_PROMPT = u"""你是答题助手。

严格按下面的格式回复，除此之外不要输出任何内容——不要打招呼、不要说明你在做什么、不要重复题目要求、不要写"好的""根据题目"之类的话：

【题目】用一句话复述题目。多道题就分条写。
【答案】直接给答案。选择题写出选项字母和选项内容；填空/判断/计算题直接给结果。
【解析】一到两句话说明为什么，不超过50字。

其他规则：
1. 多道题时，每道题按上面三段写完，题与题之间空一行。
2. 不确定就给最可能的答案，并在答案后标 (不确定)。
3. 如果给你的内容里没有题目，只回复六个字符：NO_QUESTION"""

USER_PREFIX_TEXT = u"下面是从屏幕上 OCR 识别出来的文字，可能有错别字或断行，请自行还原后作答：\n\n"
USER_PREFIX_IMAGE = u"请作答这张截图里的题目。"

NO_QUESTION = "NO_QUESTION"
MAX_SIDE = 1400  # 视觉模式下的图片边长上限


def encode_image(img):
    w, h = img.size
    if max(w, h) > MAX_SIDE:
        scale = MAX_SIDE / float(max(w, h))
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class _Cancelled(Exception):
    """用户翻到下一题了，当前这次作废。"""


class Solver(threading.Thread):
    """单槽任务队列：新截图直接顶掉还没开始的旧任务。

    回调（都带 gen 任务号，主线程据此丢弃过期结果）：
        on_stage(gen, stage, detail)  阶段变化：ocr / ask / done
        on_ocr(gen, result)           OCR 结果 dict，供界面展示识别到的原文
        on_delta(gen, chunk)          模型流式输出
        on_done(gen, text, elapsed)
        on_error(gen, msg)
    """

    def __init__(self, cfg, on_stage, on_ocr, on_delta, on_done, on_error):
        super(Solver, self).__init__(daemon=True)
        self.cfg = cfg
        self.backend = cfg.get("backend", "text")
        self.on_stage = on_stage
        self.on_ocr = on_ocr
        self.on_delta = on_delta
        self.on_done = on_done
        self.on_error = on_error

        self._client = None
        self._lock = threading.Lock()
        self._pending = None
        self._wake = threading.Event()
        self._gen = 0
        self._stop_evt = threading.Event()

    # ---------- 客户端 ----------
    def _get_client(self):
        if self._client is not None:
            return self._client
        if self.backend == "vision":
            from anthropic import Anthropic
            key = self.cfg.get("vision", {}).get("api_key") or None
            vis = self.cfg.get("vision", {})
            timeout = float(vis.get("timeout", 60))
            self._client = (Anthropic(api_key=key, timeout=timeout) if key
                            else Anthropic(timeout=timeout))
        else:
            from openai import OpenAI
            api = self.cfg.get("api", {})
            key = api.get("api_key") or ""
            if not key:
                raise RuntimeError(
                    u"还没填 API key。打开 config.json，把 api.api_key 填上，"
                    u"或设置环境变量 DEEPSEEK_API_KEY。")
            # 必须显式设超时：SDK 默认 600 秒，一次卡住的请求会把整个
            # solver 线程堵死十分钟，后面的题全都排不上。
            self._client = OpenAI(
                api_key=key,
                base_url=api.get("base_url") or None,
                timeout=float(api.get("timeout", 30)),
                max_retries=int(api.get("max_retries", 1)))
        return self._client

    # ---------- 任务 ----------
    def stop(self):
        self._stop_evt.set()
        self._wake.set()

    def submit(self, img):
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._pending = (gen, img)
        self._wake.set()
        return gen

    def _current_gen(self):
        with self._lock:
            return self._gen

    def _check(self, gen):
        if gen != self._current_gen() or self._stop_evt.is_set():
            raise _Cancelled()

    def run(self):
        # 提前把 OCR 引擎加载好，别让第一题白等一两秒
        if self.backend != "vision":
            self.on_stage(0, "loading", u"正在加载 OCR 引擎…")
            engine, err = ocr.load_engine()
            self.on_stage(0, "ready", err or u"OCR 引擎就绪")

        while not self._stop_evt.is_set():
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                job = self._pending
                self._pending = None
            if job is None or self._stop_evt.is_set():
                continue
            gen, img = job
            try:
                self._solve(gen, img)
            except _Cancelled:
                pass
            except Exception as exc:
                if gen == self._current_gen():
                    self.on_error(gen, "%s: %s" % (type(exc).__name__, exc))

    def _solve(self, gen, img):
        t0 = time.time()
        if self.backend == "vision":
            self.on_stage(gen, "ask", u"提问中…")
            self._ask_vision(gen, img, t0)
            return

        # --- OCR ---
        self.on_stage(gen, "ocr", u"识别文字中…")
        res = ocr.recognize(
            img,
            min_confidence=self.cfg.get("ocr", {}).get("min_confidence", 0.5),
            upscale=self.cfg.get("ocr", {}).get("upscale", 2),
        )
        self._check(gen)
        self.on_ocr(gen, res)

        if res["error"]:
            self.on_error(gen, res["error"])
            return
        if not res["text"].strip():
            self.on_done(gen, NO_QUESTION, time.time() - t0)
            return

        # --- 提问 ---
        self.on_stage(gen, "ask", u"提问中…")
        self._ask_text(gen, res["text"], t0)

    def _ask_text(self, gen, text, t0):
        api = self.cfg.get("api", {})
        client = self._get_client()
        stream = client.chat.completions.create(
            model=api.get("model", "deepseek-chat"),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PREFIX_TEXT + text},
            ],
            temperature=api.get("temperature", 0),
            max_tokens=api.get("max_tokens", 1000),
            stream=True,
        )
        acc = []
        for chunk in stream:
            self._check(gen)
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content  # 推理模型的 reasoning_content 不要
            if piece:
                acc.append(piece)
                self.on_delta(gen, piece)
        self.on_done(gen, "".join(acc), time.time() - t0)

    def _ask_vision(self, gen, img, t0):
        client = self._get_client()
        content = [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png",
                        "data": encode_image(img)}},
            {"type": "text", "text": USER_PREFIX_IMAGE},
        ]
        acc = []
        with client.messages.stream(
            model=self.cfg.get("vision", {}).get("model", "claude-sonnet-5"),
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        ) as stream:
            for piece in stream.text_stream:
                self._check(gen)
                acc.append(piece)
                self.on_delta(gen, piece)
        self.on_done(gen, "".join(acc), time.time() - t0)

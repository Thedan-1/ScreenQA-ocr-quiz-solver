# -*- coding: utf-8 -*-
"""区域截屏 + 内容变化检测。

核心思路：不用固定间隔去问模型，而是盯着区域的画面指纹。
画面一动 -> 进入“翻页中”；动完并且连续 N 帧不再变 -> 认为新题目已经稳定，这时才触发识别。
所以翻题快慢都无所谓。
"""

import threading
import time

import numpy as np
from PIL import Image

try:                      # mss 9.1+ 用 MSS，旧版是 mss.mss
    from mss import MSS as _MSS
except ImportError:
    from mss import mss as _MSS

# 指纹分辨率：64x64 灰度，足够区分题目、又便宜
_SIG_SIZE = (64, 64)


def _signature(img):
    """把截图压成一个小的灰度矩阵，用来比较画面是否变了。"""
    small = img.convert("L").resize(_SIG_SIZE, Image.BILINEAR)
    return np.asarray(small, dtype=np.int16)


def _diff_ratio(a, b):
    """返回“明显变化的像素占比”。抗噪：单像素亮度差要超过 12 才算变了。"""
    if a is None or b is None:
        return 1.0
    return float(np.mean(np.abs(a - b) > 12))


class RegionWatcher(threading.Thread):
    """在后台线程里盯住一块屏幕区域，稳定后回调 on_settled(PIL.Image)。"""

    def __init__(self, region, on_settled, on_state=None, poll_interval=0.25,
                 diff_threshold=0.012, stable_frames=2, min_gap=0.8):
        super(RegionWatcher, self).__init__(daemon=True)
        self.region = dict(region)
        self.on_settled = on_settled
        self.on_state = on_state or (lambda s: None)
        self.poll_interval = poll_interval
        self.diff_threshold = diff_threshold
        self.stable_frames = stable_frames
        self.min_gap = min_gap  # 两次触发之间的最小间隔，防抖

        self._stop_evt = threading.Event()
        self._force_evt = threading.Event()
        self.paused = False

        self._prev_sig = None      # 上一帧
        self._answered_sig = None  # 最近一次已经送去识别的画面
        self._moving = True        # 启动时当作“刚翻页”，这样第一题会被自动识别
        self._stable = 0
        self._last_fire = 0.0

    def stop(self):
        self._stop_evt.set()

    def force(self):
        """手动要求重新识别当前画面（F9）。"""
        self._force_evt.set()

    def set_region(self, region):
        self.region = dict(region)
        self._prev_sig = None
        self._answered_sig = None
        self._moving = True
        self._stable = 0

    def run(self):
        with _MSS() as sct:
            while not self._stop_evt.is_set():
                t0 = time.time()
                try:
                    self._tick(sct)
                except Exception as exc:  # 截屏偶发失败（切换分辨率/锁屏等），不要让线程死掉
                    self.on_state("capture error: %s" % exc)
                    time.sleep(0.5)
                sleep = self.poll_interval - (time.time() - t0)
                if sleep > 0:
                    self._stop_evt.wait(sleep)

    def _grab(self, sct):
        shot = sct.grab(self.region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def _tick(self, sct):
        forced = self._force_evt.is_set()
        if self.paused and not forced:
            return

        img = self._grab(sct)
        sig = _signature(img)

        if forced:
            self._force_evt.clear()
            self._fire(img, sig)
            return

        if self._prev_sig is None:
            self._prev_sig = sig
            return

        moved = _diff_ratio(sig, self._prev_sig) > self.diff_threshold
        self._prev_sig = sig

        if moved:
            # 画面还在动（翻页动画、滚动、加载）——等它停下来
            self._moving = True
            self._stable = 0
            self.on_state("翻页中…")
            return

        if not self._moving:
            return

        self._stable += 1
        if self._stable < self.stable_frames:
            return

        # 稳定了。和上次已识别的画面比一下，一样就不重复问
        if _diff_ratio(sig, self._answered_sig) <= self.diff_threshold:
            self._moving = False
            self._stable = 0
            return

        if time.time() - self._last_fire < self.min_gap:
            return

        self._fire(img, sig)

    def _fire(self, img, sig):
        self._moving = False
        self._stable = 0
        self._answered_sig = sig
        self._last_fire = time.time()
        self.on_settled(img)

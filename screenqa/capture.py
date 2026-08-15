# -*- coding: utf-8 -*-
"""区域截屏 + 内容变化检测。

核心思路：不用固定间隔去问模型，而是盯着区域的画面指纹。
画面一动 -> 进入“翻页中”；动完并且连续 N 帧不再变 -> 认为新题目已经稳定，
这时才触发识别。所以翻题快慢都无所谓。

性能上有两点实测结论（以 2247x1195 的区域为例）：
  1. sct.grab 有约 16.6ms 的固定开销（≈1/60 秒，卡在桌面合成器上），
     面积只贡献另外约 16.5ms。所以抓小块最多省一半，抓多次反而更慢。
  2. 用 PIL 走「全尺寸 -> 灰度 -> 缩放」算指纹要 11ms；直接在原始 BGRA
     缓冲区上用 numpy 抽样只要 2ms。

于是：指纹走 numpy；空闲时只抓一条横带做预检，有动静或每隔几拍才抓全幅。
"""

import threading
import time

import numpy as np
from PIL import Image

try:                      # mss 9.1+ 用 MSS，旧版是 mss.mss
    from mss import MSS as _MSS
except ImportError:
    from mss import mss as _MSS

_SIG = 64           # 指纹边长
_NOISE = 12         # 单像素亮度差超过这个值才算“变了”，滤掉抗锯齿和轻微重绘
_PROBE_MIN_H = 400  # 区域矮于这个高度时预检省不下时间，直接全幅


def _sig_from_bgra(buf, width, height, size=_SIG):
    """直接在原始 BGRA 缓冲区上抽样出灰度指纹。

    累加必须用 uint32：255*256 会溢出 int16，这里踩过一次。
    """
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4)
    ys = np.linspace(0, height - 1, size).astype(np.intp)
    xs = np.linspace(0, width - 1, size).astype(np.intp)
    sub = arr[np.ix_(ys, xs)][:, :, :3].astype(np.uint32)
    lum = (sub[:, :, 2] * 77 + sub[:, :, 1] * 150 + sub[:, :, 0] * 29) >> 8
    return lum.astype(np.int16)


def _sig_of_shot(shot, size=_SIG):
    return _sig_from_bgra(shot.bgra, shot.width, shot.height, size)


def _sig_band_of_shot(shot, y0, y1, size=_SIG):
    """从整幅截图里取出横带部分的指纹，好和单独抓的横带对齐比较。"""
    arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
        shot.height, shot.width, 4)
    band = np.ascontiguousarray(arr[y0:y1])
    return _sig_from_bgra(band.tobytes(), shot.width, y1 - y0, size)


def _diff_ratio(a, b):
    """返回“明显变化的像素占比”。"""
    if a is None or b is None:
        return 1.0
    return float(np.mean(
        np.abs(a.astype(np.int32) - b.astype(np.int32)) > _NOISE))


def _to_image(shot):
    return Image.frombytes("RGB", (shot.width, shot.height), shot.bgra,
                           "raw", "BGRX")


class RegionWatcher(threading.Thread):
    """在后台线程里盯住一块屏幕区域，稳定后回调 on_settled(PIL.Image)。"""

    def __init__(self, region, on_settled, on_state=None, poll_interval=0.25,
                 diff_threshold=0.012, stable_frames=2, min_gap=0.8,
                 fast_probe=True, full_every=8):
        super(RegionWatcher, self).__init__(daemon=True)
        self.on_settled = on_settled
        self.on_state = on_state or (lambda s: None)
        self.poll_interval = poll_interval
        self.diff_threshold = diff_threshold
        self.stable_frames = stable_frames
        self.min_gap = min_gap
        self.fast_probe = fast_probe
        self.full_every = max(2, full_every)   # 空闲时每隔几拍强制全幅兜底

        self._stop_evt = threading.Event()
        self._force_evt = threading.Event()
        self.paused = False
        self.stats = {"ticks": 0, "full_grabs": 0, "probe_grabs": 0}

        self.set_region(region)

    # ---------- 区域 ----------
    def set_region(self, region):
        self.region = dict(region)
        h = self.region["height"]
        # 预检横带取区域中部。实测 grab 耗时在 25%~50% 面积区间几乎不变
        # （16.5ms vs 17.5ms，固定开销占大头），所以横带直接取到 60% 高度：
        # 成本和取 1/3 差不多，覆盖率却高得多。
        # 万一变化仍然完全避开这一带，还有 full_every 的全幅兜底。
        if self.fast_probe and h >= _PROBE_MIN_H:
            bh = max(160, int(h * 0.6))
            self._band_y0 = (h - bh) // 2
            self._band_y1 = self._band_y0 + bh
            self.probe_region = {
                "left": self.region["left"],
                "top": self.region["top"] + self._band_y0,
                "width": self.region["width"],
                "height": bh,
            }
        else:
            self.probe_region = None
        self._reset_state()

    def _reset_state(self):
        self._prev_sig = None       # 上一帧全幅指纹
        self._answered_sig = None   # 最近一次已送去识别的画面
        self._probe_sig = None      # 上一帧横带指纹
        self._moving = True         # 启动时当作刚翻页，第一题会被自动识别
        self._stable = 0
        self._since_full = 0
        self._last_fire = 0.0

    # ---------- 控制 ----------
    def stop(self):
        self._stop_evt.set()

    def force(self):
        """手动要求重新识别当前画面（F9）。"""
        self._force_evt.set()

    # ---------- 主循环 ----------
    def run(self):
        with _MSS() as sct:
            while not self._stop_evt.is_set():
                t0 = time.time()
                try:
                    self._tick(sct)
                except Exception as exc:  # 切分辨率、锁屏等会让截屏失败，别让线程死掉
                    self.on_state("capture error: %s" % exc)
                    time.sleep(0.5)
                sleep = self.poll_interval - (time.time() - t0)
                if sleep > 0:
                    self._stop_evt.wait(sleep)

    def _grab_full(self, sct):
        self.stats["full_grabs"] += 1
        return sct.grab(self.region)

    def _tick(self, sct):
        self.stats["ticks"] += 1
        forced = self._force_evt.is_set()
        if self.paused and not forced:
            return

        if forced:
            self._force_evt.clear()
            self._fire(self._grab_full(sct))
            return

        # 空闲且启用预检：只抓中间那条横带，成本约为全幅的一半
        if (self.probe_region is not None and not self._moving
                and self._since_full < self.full_every):
            self.stats["probe_grabs"] += 1
            psig = _sig_of_shot(sct.grab(self.probe_region))
            self._since_full += 1
            if self._probe_sig is not None and \
                    _diff_ratio(psig, self._probe_sig) > self.diff_threshold:
                self._moving = True       # 有动静，下一拍起走全幅
                self._stable = 0
                self.on_state(u"翻页中…")
            self._probe_sig = psig
            return

        # 全幅路径
        shot = self._grab_full(sct)
        self._since_full = 0
        sig = _sig_of_shot(shot)
        if self.probe_region is not None:
            self._probe_sig = _sig_band_of_shot(shot, self._band_y0,
                                                self._band_y1)

        if self._prev_sig is None:
            self._prev_sig = sig
            return

        moved = _diff_ratio(sig, self._prev_sig) > self.diff_threshold
        self._prev_sig = sig

        if moved:
            self._moving = True
            self._stable = 0
            self.on_state(u"翻页中…")
            return

        if not self._moving:
            return

        self._stable += 1
        if self._stable < self.stable_frames:
            return

        # 稳定了。和上次已识别的画面比一次，一样就不重复问
        if _diff_ratio(sig, self._answered_sig) <= self.diff_threshold:
            self._moving = False
            self._stable = 0
            return

        if time.time() - self._last_fire < self.min_gap:
            return

        self._fire(shot)

    def _fire(self, shot):
        self._moving = False
        self._stable = 0
        self._answered_sig = _sig_of_shot(shot)
        self._prev_sig = self._answered_sig
        self._last_fire = time.time()
        self.on_settled(_to_image(shot))

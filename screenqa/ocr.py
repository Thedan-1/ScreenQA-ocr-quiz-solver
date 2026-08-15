# -*- coding: utf-8 -*-
"""本地 OCR：把截图转成文字，交给纯语言模型。

用 RapidOCR（ONNX 版），自带中英文模型，不依赖 paddle / torch。
引擎第一次加载要一两秒，所以做成懒加载并单例复用。
"""

import io
import threading
import time

_engine = None
_engine_lock = threading.Lock()
_engine_error = None


def load_engine():
    """加载 OCR 引擎（线程安全，只加载一次）。返回 (engine, error_msg)。"""
    global _engine, _engine_error
    with _engine_lock:
        if _engine is not None or _engine_error is not None:
            return _engine, _engine_error
        try:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        except Exception as exc:
            _engine_error = "OCR 引擎加载失败：%s: %s" % (type(exc).__name__, exc)
        return _engine, _engine_error


def _group_lines(items):
    """把 OCR 的碎块按行还原：先按纵向位置分行，行内再按横向排序。"""
    rows = []
    for box, text, score in items:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        rows.append({
            "cy": sum(ys) / 4.0,
            "x": min(xs),
            "h": max(ys) - min(ys),
            "text": text,
            "score": score,
        })
    rows.sort(key=lambda r: r["cy"])

    lines, cur = [], []
    for r in rows:
        if not cur:
            cur = [r]
            continue
        # 行高的 60% 作为容差，避免上下标、括号被拆到别的行
        tol = max(6.0, cur[-1]["h"] * 0.6)
        if abs(r["cy"] - cur[0]["cy"]) <= tol:
            cur.append(r)
        else:
            lines.append(cur)
            cur = [r]
    if cur:
        lines.append(cur)

    out = []
    for ln in lines:
        ln.sort(key=lambda r: r["x"])
        out.append(" ".join(r["text"] for r in ln))
    return "\n".join(out)


def recognize(img, min_confidence=0.5, upscale=2, min_side=500):
    """识别一张 PIL 图片。

    返回 dict: text / n_chars / n_blocks / confidence / elapsed / error
    """
    engine, err = load_engine()
    if engine is None:
        return {"text": "", "n_chars": 0, "n_blocks": 0, "confidence": 0.0,
                "elapsed": 0.0, "error": err}

    t0 = time.time()
    try:
        # 题目区域通常不大，放大一点显著提升小字识别率
        w, h = img.size
        if upscale > 1 and min(w, h) < min_side:
            from PIL import Image
            img = img.resize((w * upscale, h * upscale), Image.LANCZOS)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        result, _ = engine(buf.getvalue())
    except Exception as exc:
        return {"text": "", "n_chars": 0, "n_blocks": 0, "confidence": 0.0,
                "elapsed": time.time() - t0,
                "error": "%s: %s" % (type(exc).__name__, exc)}

    if not result:
        return {"text": "", "n_chars": 0, "n_blocks": 0, "confidence": 0.0,
                "elapsed": time.time() - t0, "error": None}

    kept = [(box, txt, score) for box, txt, score in result
            if score >= min_confidence and txt.strip()]
    if not kept:
        return {"text": "", "n_chars": 0, "n_blocks": 0, "confidence": 0.0,
                "elapsed": time.time() - t0, "error": None}

    text = _group_lines(kept)
    avg = sum(s for _, _, s in kept) / float(len(kept))
    return {
        "text": text,
        "n_chars": len(text.replace("\n", "")),
        "n_blocks": len(kept),
        "confidence": avg,
        "elapsed": time.time() - t0,
        "error": None,
    }

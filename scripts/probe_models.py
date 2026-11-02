"""Діагностика рушія: DML-провайдер, швидкість тайлів, ліanal-якість моделей.

    python scripts/probe_models.py

Створює tests_out/comparison.png (оригінал · бікубік · Real-ESRGAN) для
візуальної перевірки якості моделей ONNX.
"""
from __future__ import annotations

import io
import os
import sys
import time
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
OUT = ROOT / "tests_out"


def probe_dml_cpu():
    rng = np.random.default_rng(0)
    x = rng.random((1, 3, 128, 128)).astype("float32")
    so = ort.SessionOptions()
    so.log_severity_level = 3

    sess_dml = ort.InferenceSession(
        str(MODELS / "realesrgan_x4plus_fp16.onnx"), so,
        providers=[("DmlExecutionProvider", {"device_id": 0})])
    sess_cpu = ort.InferenceSession(
        str(MODELS / "realesrgan_x4plus_fp16.onnx"), so,
        providers=["CPUExecutionProvider"])
    o_dml = sess_dml.run(None, {"input": x})[0]
    o_cpu = sess_cpu.run(None, {"input": x})[0]
    d = np.abs(np.clip(o_dml, 0, 1) - np.clip(o_cpu, 0, 1))
    print(f"DML vs CPU fp16: max {d.max():.5f}, mean {d.mean():.6f}  (має бути < 0.02)")
    assert np.isfinite(o_dml).all(), "DML out contains NaN/Inf!"


def _in(sess) -> str:
    return sess.get_inputs()[0].name


def tile_time(sess, px: int, n: int = 3):
    x = np.random.default_rng(1).random((1, 3, px, px)).astype("float32")
    ins = _in(sess)
    ts = []
    for _ in range(n):
        t0 = time.time()
        sess.run(None, {ins: x})
        ts.append(time.time() - t0)
    return min(ts)


def main():
    probe_dml_cpu()
    so = ort.SessionOptions()
    so.log_severity_level = 3
    dml_opts = {"device_id": 0}
    s_x4 = ort.InferenceSession(str(MODELS / "realesrgan_x4plus_fp16.onnx"), so,
                                providers=[("DmlExecutionProvider", dml_opts)])
    s_x2 = ort.InferenceSession(str(MODELS / "realesrgan_x2plus.onnx"), so,
                                providers=[("DmlExecutionProvider", dml_opts)])
    s_x4c = ort.InferenceSession(str(MODELS / "realesrgan_x4plus.onnx"), so,
                                 providers=["CPUExecutionProvider"])

    print("x4 fp16 DML 512px:", f"{tile_time(s_x4, 512):.2f}s")
    print("x2 fp32 DML 512px:", f"{tile_time(s_x2, 512):.2f}s")

    # --- візуальна перевірка реальною картинкою ---
    OUT.mkdir(exist_ok=True)
    w, h = 260, 170
    rng = np.random.default_rng(42)
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.stack([yy / h * 255, xx / w * 220 + 35, np.full((h, w), 150)], 2)
    arr = np.clip(base + rng.normal(0, 16, (h, w, 3)), 0, 255).astype("uint8")
    img = Image.fromarray(arr)
    d = ImageDraw.Draw(img)
    d.ellipse((95, 60, 165, 130), outline=(255, 240, 10), width=2)
    d.rectangle((20, 20, 90, 70), outline=(255, 90, 200), width=2)
    d.text((110, 140), "detail test", fill=(255, 255, 255))

    x = np.asarray(img, np.float32).transpose(2, 0, 1)[None] / 255.0

    o4 = np.clip(s_x4.run(None, {_in(s_x4): x})[0][0], 0, 1)
    o2 = np.clip(s_x2.run(None, {_in(s_x2): x})[0][0], 0, 1)

    # кореляція x2-моделі з даунскейлом x4-результату (обидві «бачать» те ж)
    x4_down = np.asarray(
        Image.fromarray((o4.transpose(1, 2, 0) * 255).astype("uint8")).resize((w * 2, h * 2), Image.LANCZOS),
        np.float32).transpose(2, 0, 1) / 255.0
    a = x4_down.reshape(-1)
    b = o2.reshape(-1)
    corr = np.corrcoef(a, b)[0, 1]
    print(f"кореляція x2-моделі з даунскейлом x4: {corr:.4f}  (має бути > 0.95)")
    assert np.isfinite(o2).all()

    bic = img.resize((w * 4, h * 4), Image.BICUBIC)
    nn = Image.fromarray((o4.transpose(1, 2, 0) * 255).astype("uint8"))
    nn2 = Image.fromarray((o2.transpose(1, 2, 0) * 255).astype("uint8"))

    canvas = Image.new("RGB", (w * 4, (h * 4) * 2 + 20), (20, 22, 34))
    canvas.paste(bic, (0, 0))
    canvas.paste(nn, (0, h * 4 + 20))
    canvas.save(OUT / "comparison_x4.png")
    canvas2 = Image.new("RGB", (w * 2, (h * 2) * 2 + 20), (20, 22, 34))
    canvas2.paste(img.resize((w * 2, h * 2), Image.BICUBIC), (0, 0))
    canvas2.paste(nn2, (0, h * 2 + 20))
    canvas2.save(OUT / "comparison_x2.png")
    print("збережено:", OUT / "comparison_x4.png", "та comparison_x2.png (верх: бікубік, низ: Real-ESRGAN)")


if __name__ == "__main__":
    main()

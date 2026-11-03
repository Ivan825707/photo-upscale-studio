"""Генерація іконки assets/app.ico (масштаб з підказкою на переповнення)."""
from __future__ import annotations
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image, ImageDraw

OUT = Path("assets/app.ico")
OUT.parent.mkdir(exist_ok=True)

BASE = 256
img = Image.new("RGBA", (BASE, BASE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# скруглена плитка
d.rounded_rectangle((12, 12, 244, 244), radius=44,
                    fill=(24, 28, 44, 255), outline=(96, 110, 180, 255),
                    width=6)

# зростаючі рамки (символ апскейлу) — попереджувально-порядковий градієнт
frames = [
    (52, 150, 132, 204, (120, 132, 200, 255)),   # велика (найменш якісна)
    (84, 110, 164, 218, None),                   # placeholder: переробимо нижче
]


def frame(x0, y0, x1, y1, color):
    d.rounded_rectangle((x0, y0, x1, y1), radius=12, outline=color, width=10)


frame(56, 146, 132, 202, (94, 108, 176, 255))
frame(96, 102, 172, 158, (140, 156, 224, 255))

# кутова стрілка вгору-праворуч (зростання)
ax, ay = 176, 96
d.line((160, 196, 208, 148), fill=(240, 214, 120, 255), width=14)
d.polygon([(204, 146), (214, 190), (170, 180)], fill=(240, 214, 120, 255))

img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                     (64, 64), (128, 128), (256, 256)])
print("OK:", OUT, OUT.stat().st_size, "bytes")

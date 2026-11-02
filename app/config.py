"""Центральні налаштування Photo Upscale Studio."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
MODELS_DIR = ROOT / "models"          # ваги моделей (завантажуються першого разу)
DATA_DIR = ROOT / "data"              # сесії: завантажені фото, результати, перегляди

VERSION = "1.0.0"
APP_TITLE = "Photo Upscale Studio"

# --- обмеження, зрозумілі користувачу ---
MAX_PHOTOS = 5                                     # фото за одну сесію
MAX_INPUT_FILE_MB = 100                            # максимальний розмір одного файлу
MAX_OUTPUT_MP = float(os.environ.get("UPSCALE_MAX_OUTPUT_MP", "256"))  # ліміт результату, Мп
MIN_IMAGE_DIM = 4                                  # мінімальна сторона зображення

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
OUTPUT_FORMATS = {"png", "jpeg", "webp"}
DEFAULT_FORMAT = "jpeg"
DEFAULT_SCALE = 8
OUTPUT_QUALITY = {"jpeg": 95, "webp": 95}

# --- тайлова інференс: розмір тайла (вхідні пікселі) залежно від провайдера ---
DEVICE_TILE = {"dml": 512, "cuda": 512, "cpu": 384}
TILE_OVERLAP = 24                                   # нахлест тайлів, щоб не було швів (парне!)

# --- завантаження моделей ---
DOWNLOAD_RETRIES = 3
DOWNLOAD_TIMEOUT = (15, 300)                        # (connect, read), сек

# Пояснення походження файлів:
#  * realesrgan_x4plus{,_fp16}.onnx — прозорий експорт офіційних вагів
#    RealESRGAN_x4plus.pth (SHA-256 4fa0d389…): ONNX-конвертацію опубліковано
#    з перевіреними хешами (репозиторій huggingface.co/othytenk/realesrgan-x4plus,
#    BSD-3-Clause). Хеші файлів зафіксовані нижче та перевіряються при
#    завантаженні.
#  * realesrgan_x2plus.onnx — конверсія офіційних RealESRGAN_x2plus.pth
#    з репозиторію huggingworld/onnx-image-models; якість верифікована
#    порівняльним тестом рушія (скрипт scripts/probe_models.py).
MD = "https://huggingface.co"
MODEL_SPECS: dict[str, dict] = {
    "x4": {
        "title": "Модель 4× (Real-ESRGAN x4plus)",
        "scale": 4,
        "files": {"gpu": "realesrgan_x4plus_fp16.onnx", "cpu": "realesrgan_x4plus.onnx"},
        "files_meta": {
            "realesrgan_x4plus_fp16.onnx": {
                "url": f"{MD}/othytenk/realesrgan-x4plus/resolve/main/realesrgan_x4plus_fp16.onnx",
                "sha256": "9c2b0750c659982c3f98ee32900def52556f945b15dd1f3b81f26baf7390e94f",
            },
            "realesrgan_x4plus.onnx": {
                "url": f"{MD}/othytenk/realesrgan-x4plus/resolve/main/realesrgan_x4plus.onnx",
                "sha256": "ec90f9f71386729be0d7ffb324ac4c61a55f3539c8e7ff42ab57984952005ce9",
            },
        },
    },
    "x2": {
        "title": "Модель 2× (Real-ESRGAN x2plus)",
        "scale": 2,
        "files": {"gpu": "realesrgan_x2plus.onnx", "cpu": "realesrgan_x2plus.onnx"},
        "files_meta": {
            "realesrgan_x2plus.onnx": {
                "url": f"{MD}/huggingworld/onnx-image-models/resolve/main/2x-realesrgan-x2plus.onnx",
                "sha256": "c4c0b7430ebb554f3939a720f9e8445fdeca3d15edb69979c7ace39b997f3483",
            },
        },
    },
}

SESSION_IDLE_HOURS = 8       # сесії старші за це — видаляються прибиральником
DEFAULT_PORT = int(os.environ.get("PORT", "8560"))
MAX_PORT_ATTEMPTS = 20       # скільки портів спробувати, якщо 8560 зайнятий

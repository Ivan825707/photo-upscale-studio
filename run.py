"""Запуск Photo Upscale Studio.

    python run.py                 → http://127.0.0.1:8560
    python run.py --port 9000     → інший порт
    python run.py --host 0.0.0.0  → доступ з інших пристроїв у локальній мережі
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser

TIMEOUT_HINT = ""


def _friendly_import_error() -> None:
    """Друкує зрозуміле пояснення, якщо залежності не встановлені."""
    missing = []
    for mod, pkg in [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("PIL", "pillow"),
        ("numpy", "numpy"),
        ("requests", "requests"),
        ("onnxruntime", "onnxruntime(-directml)"),
    ]:
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    if missing:
        print("=" * 62)
        print("НЕ ВСТАНОВЛЕНІ ЗАЛЕЖНОСТІ:", ", ".join(missing))
        print("Виконайте у папці проекту:")
        print("    pip install -r requirements.txt")
        print("=" * 62)
        sys.exit(1)


def _check_python() -> None:
    if sys.version_info < (3, 10):
        print("Потрібен Python 3.10 або новіший. Встановлено:", sys.version)
        sys.exit(1)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _pick_port(start: int, attempts: int) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return start  # повертаємо початковий — uvicorn сам покаже помилку


def main() -> None:
    _check_python()
    try:
        _friendly_import_error()
    except SystemExit:
        raise

    parser = argparse.ArgumentParser(description="Photo Upscale Studio")
    parser.add_argument("--host", default="127.0.0.1", help="адреса сервера")
    parser.add_argument("--port", type=int, default=None, help="порт (типово 8560)")
    parser.add_argument("--no-browser", action="store_true", help="не відкривати браузер")
    args = parser.parse_args()

    from app import config
    from app.main import create_app

    app = create_app()
    engine = app.state.engine  # тип: Engine (створюється при ініціалізації)

    port = args.port or _pick_port(config.DEFAULT_PORT, config.MAX_PORT_ATTEMPTS)
    url = f"http://{args.host}:{port}/"

    print()
    print("=" * 62)
    print("  Photo Upscale Studio — локальний апскейл фото (Real-ESRGAN)")
    print("=" * 62)
    print(f"  Адреса:        {url}")
    print(f"  Обчислення:    {engine.device_label}")
    print(f"  Тайл:          {engine.tile}px з нахлестом {24}px")
    print("  Перша обробка завантажить моделі (~100 МБ) з Hugging Face.")
    print("  Зупинка: Ctrl+C у цьому вікні.")
    print("=" * 62)
    print()

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()

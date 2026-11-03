"""Photo Upscale Studio — настільний запуск (нативне вікно без браузера).

Сервер працює всередині процесу, інтерфейс показується у вікні WebView2
(рушій Edge, що вбудований у Windows). Закриття вікна зупиняє сервер.

Розробка:
    python desktop.py

Збірка в .exe (докладніше в scripts/build_exe.py):
    .\\.venv\\Scripts\\python.exe scripts\\build_exe.py
"""
from __future__ import annotations

import socket
import sys
import threading
import time


def _pick_port(start: int, attempts: int) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return start


def _wait_url(url: str, timeout: float = 20.0) -> bool:
    try:
        import urllib.request
    except Exception:                                   # pragma: no cover
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from app import config
    from app.main import create_app

    app = create_app()
    port = _pick_port(config.DEFAULT_PORT, config.MAX_PORT_ATTEMPTS)
    url = f"http://127.0.0.1:{port}/"

    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True, name="http-server")
    t.start()

    # перший показ вікна чекає сервіс; якщо не піднявся — все одно відкриємо,
    # WebView покаже помилку і користувач зможе перезапустити програму
    _wait_url(url, timeout=20.0 if srv.should_exit is False else 0)

    icon = None
    try:
        from pathlib import Path
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        ico = base / "assets" / "app.ico"
        if ico.exists():
            icon = str(ico)
    except Exception:
        pass

    import webview

    window = webview.create_window(
        f"{config.APP_TITLE} {config.VERSION}",
        url,
        width=1240,
        height=840,
        min_size=(960, 640),
        background_color="#0b0d14",
    )
    try:
        webview.start(icon=icon)
    finally:
        srv.should_exit = True
        t.join(timeout=5)
        # жорсткий вихід, щоб не зависали фонові потоки ONNX Runtime
        import os
        os._exit(0)


if __name__ == "__main__":
    main()

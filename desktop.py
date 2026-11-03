"""Photo Upscale Studio — настільний запуск (нативне вікно без браузера).

Сервер працює всередині процесу, інтерфейс показується у вікні WebView2
(рушій Edge, що вбудований у Windows). Закриття вікна зупиняє сервер.

Розробка:
    python desktop.py

Збірка в .exe (докладніше в scripts/build_exe.py):
    .\\.venv\\Scripts\\python.exe scripts\\build_exe.py
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path


def _pick_port(start: int, attempts: int) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return start


def _log(msg: str) -> None:
    """Помилки запуску — у файл поруч з даними, щоб їх можна було побачити."""
    try:
        base = os.environ.get("APPDATA") or str(Path.home())
        logdir = Path(base) / "PhotoUpscaleStudio"
        logdir.mkdir(parents=True, exist_ok=True)
        with open(logdir / "desktop.log", "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


SERVER_PORT = 8560          # уточнюється в main(), читається в Api


def _srv_run(srv, port: int) -> None:
    try:
        srv.run()
    except Exception:
        _log(f"uvicorn (port {port}) впав:\n" + traceback.format_exc())


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


class Api:
    """Місток між JS у вікні та Python: нативне збереження файлів.

    У WebView2 (нативному вікні) href-завантаження можуть не спрацювати,
    тому кнопки Завантажити ведуть сюди: файл тягнеться з локального
    сервера і зберігається через звичайний діалог Windows.
    """

    def save_url(self, url: str, default_name: str = "") -> str:
        import urllib.request
        full = f"http://127.0.0.1:{SERVER_PORT}{url}"
        try:
            with urllib.request.urlopen(full, timeout=300) as resp:
                data = resp.read()
                suggested = default_name or ""          # з Content-Disposition
                if not suggested:
                    cd = resp.headers.get("Content-Disposition", "")
                    if "filename=" in cd:
                        suggested = cd.split("filename=", 1)[1] \
                            .split(";", 1)[0].strip().strip('"*') \
                            .strip('"') or ""
        except Exception as exc:
            _log(f"save_url: не вдалося отримати {full}: {exc}")
            raise
        if not suggested:
            tail = url.rstrip("/").split("/")[-1]
            suggested = tail if tail else "результат"
        try:
            folder = str(Path.home() / "Завантаження")
            if not Path(folder).exists():
                folder = str(Path.home())
        except Exception:
            folder = ""

        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG, directory=folder, save_filename=suggested)
        if not result:
            return "cancelled"
        path = result[0] if isinstance(result, (tuple, list)) else result
        if not path:
            return "cancelled"
        with open(path, "wb") as f:
            f.write(data)
        return path


def main() -> None:
    # windowed .exe: sys.stdout/stderr == None → падіння логера uvicorn
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8",
                                    errors="replace"))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        from app import config
        from app.main import create_app
    except Exception:
        _log("імпорт рушія впав:\n" + traceback.format_exc())
        raise

    try:
        app = create_app()
    except Exception:
        _log("create_app впав:\n" + traceback.format_exc())
        raise

    port = _pick_port(config.DEFAULT_PORT, config.MAX_PORT_ATTEMPTS)
    url = f"http://127.0.0.1:{port}/"
    globals()["SERVER_PORT"] = port

    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=_srv_run, args=(srv, port), daemon=True,
                         name="http-server")
    t.start()

    # перший показ вікна чекає сервіс; якщо не піднявся — все одно відкриємо,
    # WebView покаже помилку і користувач зможе перезапустити програму
    _wait_url(url, timeout=20.0 if srv.should_exit is False else 0)

    icon = None
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    ico = base / "assets" / "app.ico"
    if ico.exists():
        icon = str(ico)

    import webview

    webview.create_window(
        f"{config.APP_TITLE} {config.VERSION}",
        url,
        width=1240,
        height=840,
        min_size=(960, 640),
        background_color="#0b0d14",
        js_api=Api(),
    )
    try:
        webview.start(icon=icon)
    finally:
        srv.should_exit = True
        t.join(timeout=5)
        # жорсткий вихід, щоб не зависали фонові потоки ONNX Runtime
        os._exit(0)


if __name__ == "__main__":
    main()

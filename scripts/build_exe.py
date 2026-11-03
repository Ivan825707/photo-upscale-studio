"""Збірка Photo Upscale Studio в .exe (PyInstaller, один файл).

    .\\.venv\\Scripts\\python.exe -m pip install pyinstaller
    .\\.venv\\Scripts\\python.exe scripts\\build_exe.py

Результат: dist/PhotoUpscaleStudio.exe — один файл, який можна і до
кореня репозиторію (GitHub приймає файли до 100 МБ), і окремо.
Одноразове розпакування при запуску триває кілька секунд.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD_DIR = ROOT / "build"

APP = DIST / "PhotoUpscaleStudio.exe"
OUT_EXE = ROOT / "PhotoUpscaleStudio.exe"


def main() -> None:
    if not (ROOT / ".venv" / "Scripts" / "pyinstaller.exe").exists():
        print("PyInstaller не знайдено. Встановіть: pip install pyinstaller")
        sys.exit(1)

    args = [
        sys.executable, "-m", "PyInstaller",
        "desktop.py",
        "--name", "PhotoUpscaleStudio",
        "--onefile",                   # один файл без супутніх папок
        "--noconsole",                 # нативне вікно, без консолі
        "--icon", str(ROOT / "assets" / "app.ico"),
        "--add-data", f"{ROOT / 'static'};static",
        "--add-data", f"{ROOT / 'assets'};assets",
        "--hidden-import", "pywebview",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "app.main",
        # прибираємо очевидно непотрібне, щоб зменшити розмір
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pandas",
        "--clean",
        "--noconfirm",
    ]

    print(">>>", " ".join(args[1:]))
    subprocess.run(args, cwd=ROOT, check=True)

    if not APP.exists():
        print("Збірка не вдалася: exe не знайдено.")
        sys.exit(1)

    size = APP.stat().st_size / 1024 / 1024
    print(f"\nOK: {APP}  (~{size:.0f} МБ)")

    shutil.copyfile(APP, OUT_EXE)
    print(f"скопійовано в {OUT_EXE} для коммита у репозиторій")

    if size >= 100:
        print("УВАГА: файл >100 МБ — GitHub не прийме його без LFS.")
        sys.exit(2)


if __name__ == "__main__":
    main()

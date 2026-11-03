"""Збірка Photo Upscale Studio в .exe (PyInstaller, папка dist/PhotoUpscaleStudio).

    .\\.venv\\Scripts\\python.exe -m pip install pyinstaller
    .\\.venv\\Scripts\\python.exe scripts\\build_exe.py

Результат: dist/PhotoUpscaleStudio/PhotoUpscaleStudio.exe (+ _internal).
Розповсюджувати папку цілком (наприклад, zip), а не один файл.
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

APP = DIST / "PhotoUpscaleStudio" / "PhotoUpscaleStudio.exe"


def main() -> None:
    if shutil.which("pyinstaller", path=str(ROOT / ".venv" / "Scripts")) is None \
            and not (ROOT / ".venv" / "Scripts" / "pyinstaller.exe").exists():
        print("PyInstaller не знайдено. Встановіть: pip install pyinstaller")
        sys.exit(1)

    args = [
        sys.executable, "-m", "PyInstaller",
        "desktop.py",
        "--name", "PhotoUpscaleStudio",
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

    if APP.exists():
        size = sum(f.stat().st_size for f in APP.parent.rglob("*")
                   if f.is_file()) / 1024 / 1024
        print(f"\nOK: {APP}  (усього ~{size:.0f} МБ)")
    else:
        print("Збірка не вдалася: exe не знайдено.")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""FastAPI-сервер Photo Upscale Studio.

Ендпоінти:
 * POST /api/upload            — завантаження 1–5 фото
 * POST /api/process           — запуск апскейлу (коефіцієнт + формат)
 * GET  /api/status/{sid}      — стан/photos/прогрес/оцінка часу
 * GET  /api/photos/{sid}/{p}/thumb          — мініатюра оригіналу
 * GET  /api/photos/{sid}/{p}/orig-preview  — прев'ю оригіналу (для порівняння)
 * GET  /api/results/{sid}/{p}/download    — готовий файл
 * GET  /api/results/{sid}/{p}/preview     — прев'ю результату
 * GET  /api/results/{sid}/zip             — усі результати одним архівом
 * DELETE /api/photos/{sid}/{p}         — видалити фото до запуску
 * GET  /api/info                    — відомості про середовище
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import UnidentifiedImageError

from . import config, engine, jobs
from .engine import Engine, UserError

# --------------------------------------------------------------------------
# Стан сесій у пам'яті
# --------------------------------------------------------------------------

_lock = threading.RLock()
_sessions: dict[str, "Session"] = {}


class Photo:
    def __init__(self, pid: str, name: str, path: Path, w: int, h: int, size: int) -> None:
        self.id = pid
        self.name = name
        self.path = path
        self.w, self.h = w, h
        self.size = size
        self.state = "ready"          # ready | queued | working | done | error | removed
        self.progress = 0.0
        self.stage = ""
        self.error = ""
        self.out_w = 0
        self.out_h = 0
        self.out_size = 0
        self.out_seconds = 0.0
        self.started_at: float | None = None
        self.out_path: Path | None = None

    def to_dict(self, session: "Session") -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "w": self.w,
            "h": self.h,
            "bytes": self.size,
            "state": self.state,
            "progress": round(self.progress),
            "stage": self.stage,
        }
        if self.state == "working" and self.started_at:
            elapsed = time.time() - self.started_at
            d["elapsed"] = int(elapsed)
            p = self.progress
            if p > 3:
                d["eta"] = int(elapsed * (100 - p) / p)
        if self.state == "done":
            d.update({
                "elapsed": int(self.out_seconds),
                "out_w": self.out_w,
                "out_h": self.out_h,
                "out_bytes": self.out_size,
            })
        if self.error:
            d["error"] = self.error
        return d


class Session:
    def __init__(self, sid: str) -> None:
        self.id = sid
        self.dir = config.DATA_DIR / "sessions" / sid
        (self.dir / "input").mkdir(parents=True, exist_ok=True)
        (self.dir / "out").mkdir(parents=True, exist_ok=True)
        (self.dir / "thumbs").mkdir(parents=True, exist_ok=True)
        self.photos: dict[str, Photo] = {}   # зберігаємо порядок завантаження
        self.scale = config.DEFAULT_SCALE
        self.fmt = config.DEFAULT_FORMAT
        self.zip_path: Path | None = None
        self.last_active = time.time()

    def touch(self) -> None:
        self.last_active = time.time()

    def invalidate_zip(self) -> None:
        if self.zip_path and self.zip_path.exists():
            try:
                self.zip_path.unlink()
            except OSError:
                pass
        self.zip_path = None


# --------------------------------------------------------------------------
# Допоміжне
# --------------------------------------------------------------------------

def _err(msg: str, code: int = 400) -> HTTPException:
    return HTTPException(status_code=code, detail=msg)


def _safe_name(name: str, limit: int = 120) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip() or "photo"
    name = "".join(ch if (ch.isalnum() or ch in " .-_()") else "_" for ch in name)
    return name[:limit] or "photo"


def _get_session(sid: str) -> Session:
    with _lock:
        sess = _sessions.get(sid)
        if sess is None:
            raise _err("Сесію не знайдено (можливо, перезавантажити сторінку)", 404)
        sess.touch()
        return sess


def _get_photo(sess: Session, pid: str) -> Photo:
    with _lock:
        photo = sess.photos.get(pid)
        if photo is None or photo.state == "removed":
            raise _err("Фото не знайдено", 404)
        # повертаємо поза блокуванням — Photo.read-only тут ок
    return photo


def _make_thumb(ph: Photo, sess: Session) -> None:
    from PIL import Image, ImageOps
    dst = sess.dir / "thumbs" / f"{ph.id}.jpg"
    if dst.exists():
        return
    with Image.open(ph.path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        im.thumbnail((256, 256), Image.LANCZOS)
        engine._flatten(im).save(dst, format="JPEG", quality=85)


def _build_zip(sess: Session) -> Path | None:
    with _lock:
        if sess.zip_path and sess.zip_path.exists():
            return sess.zip_path
    done = [p for p in sess.photos.values() if p.state == "done" and p.out_path]
    if not done:
        return None
    zpath = sess.dir / f"upscaled_x{sess.scale}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for i, ph in enumerate(done, start=1):
            name = _unique_zip_name(z, f"{i:02d}_{_safe_name(Path(ph.name).stem, 60)}"
                                        f"_x{sess.scale}{ph.out_path.suffix}")
            z.write(ph.out_path, name)
    with _lock:
        sess.zip_path = zpath
    return zpath


def _unique_zip_name(z: zipfile.ZipFile, name: str) -> str:
    if name not in z.namelist():
        return name
    stem, _, ext = name.rpartition(".")
    i = 2
    while (f"{stem} ({i}).{ext}") in z.namelist():
        i += 1
    return f"{stem} ({i}).{ext}"


# --------------------------------------------------------------------------
# Фонові зобов'язання
# --------------------------------------------------------------------------

def _run_photo(sess: "Session", ph: "Photo", eng: "Engine", runner: "jobs.JobRunner") -> None:
    ext = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}[sess.fmt]
    out_file = sess.dir / "out" / f"{ph.id}_x{sess.scale}{ext}"

    def job() -> None:
        if ph.state != "queued":          # видалено/скасовано поки чекали чергу
            return
        with _lock:
            ph.state = "working"
            ph.started_at = time.time()
            ph.progress = 1.0
            ph.stage = "Підготовка…"
        try:
            meta = eng.upscale(ph.path, out_file, sess.scale, sess.fmt, _on_progress(ph))
        except UserError as exc:
            with _lock:
                ph.state = "error"
                ph.error = str(exc)
                ph.progress = 0.0
                ph.stage = ""
            sess.invalidate_zip()
        except MemoryError:
            with _lock:
                ph.state = "error"
                ph.error = ("Зображення вийшло надто великим для пам'яті цього ПК. "
                            "Зменшіть коефіцієнт або запустіть на ПК з більше RAM.")
                ph.progress = 0.0
            sess.invalidate_zip()
        except Exception:
            traceback.print_exc()
            with _lock:
                ph.state = "error"
                ph.error = "Внутрішня помилка обробки — див. консоль програми."
                ph.progress = 0.0
            sess.invalidate_zip()
        else:
            with _lock:
                ph.state = "done"
                ph.out_path = out_file
                ph.out_w, ph.out_h = meta["out_w"], meta["out_h"]
                ph.out_seconds = meta["seconds"]
                ph.progress = 100.0
                ph.stage = ""
                try:
                    ph.out_size = out_file.stat().st_size
                    engine.make_preview(out_file, sess.dir / "out" / f"{ph.id}_preview.jpg")
                except Exception:
                    traceback.print_exc()
            sess.invalidate_zip()

    runner.submit(job)


def _on_progress(ph: "Photo"):
    def cb(phase: str, pct: float, note: str = "") -> None:
        with _lock:
            ph.progress = max(ph.progress, min(pct, 100.0))
            ph.stage = note
    return cb


def _prune_loop() -> None:
    """Періодичне прибирання сесій, до яких давно не обращались."""
    while True:
        time.sleep(15 * 60)
        with _lock:
            now = time.time()
            stale = [sid for sid, s in _sessions.items()
                     if now - s.last_active > config.SESSION_IDLE_HOURS * 3600]
            for sid in stale:
                s = _sessions.pop(sid)
                shutil.rmtree(s.dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Створення застосунку
# --------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(title=config.APP_TITLE, docs_url=None, redoc_url=None)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.joinpath("sessions").mkdir(exist_ok=True)

    eng = Engine()
    runner = jobs.JobRunner(workers=1)
    threading.Thread(target=_prune_loop, daemon=True, name="pruner").start()

    app.state.engine = eng

    # ---------------- сервісні ----------------

    @app.get("/api/info")
    def info() -> dict:
        return {
            "app": config.APP_TITLE,
            "version": config.VERSION,
            "device": eng.device_label,
            "gpu": eng.provider != "cpu",
            "tile": eng.tile,
            "max_photos": config.MAX_PHOTOS,
            "max_file_mb": config.MAX_INPUT_FILE_MB,
            "max_output_mp": config.MAX_OUTPUT_MP,
            "formats": sorted(config.OUTPUT_FORMATS),
            "scale_range": [1, 8],
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(config.STATIC_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

    # ---------------- завантаження ----------------

    @app.post("/api/upload")
    async def upload(
        files: list[UploadFile] = File(...),
        session: str = Form(None),
    ) -> JSONResponse:
        if not files:
            raise _err("Не надіслано жодного файлу.")

        with _lock:
            if session and session in _sessions:
                sess = _sessions[session]
            else:
                sid = uuid.uuid4().hex[:12]
                sess = Session(sid)
                _sessions[sid] = sess
            if len(files) + sum(1 for p in sess.photos.values() if p.state != "removed") > config.MAX_PHOTOS:
                raise _err(
                    f"Максимум {config.MAX_PHOTOS} фотографій за раз. "
                    f"Зайві файли відхилено (надіслано б {len(files) + sum(1 for p in sess.photos.values() if p.state != 'removed')})."
                )
            sess.touch()

        accepted: list[dict] = []
        errors: list[dict] = []

        for up in files:
            pid = uuid.uuid4().hex[:12]
            orig_name = _safe_name(up.filename or "photo")
            ext = Path(orig_name).suffix.lower()
            if ext not in config.ALLOWED_EXTENSIONS:
                errors.append({"name": orig_name,
                               "error": "Підтримуються лише JPG, PNG та WEBP"})
                continue

            dest = sess.dir / "input" / f"{pid}{ext}"
            size = 0
            too_big = False
            try:
                with open(dest, "wb") as f:
                    while True:
                        chunk = await up.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > config.MAX_INPUT_FILE_MB * 1024 * 1024:
                            too_big = True
                            break
                        f.write(chunk)
            except Exception:
                pass
            finally:
                await up.close()

            if too_big:
                dest.unlink(missing_ok=True)
                errors.append({"name": orig_name,
                               "error": f"Файл більший за {config.MAX_INPUT_FILE_MB} МБ"})
                continue

            ph = None
            try:
                from PIL import Image, ImageOps
                with Image.open(dest) as im:
                    im2 = ImageOps.exif_transpose(im)
                    w, h = im2.size
                if min(w, h) < config.MIN_IMAGE_DIM:
                    raise ValueError("надто мала роздільність")
                ph = Photo(pid, orig_name, dest, w, h, size)
            except UnidentifiedImageError:
                dest.unlink(missing_ok=True)
                errors.append({"name": orig_name, "error": "Файл не є допустимим зображенням"})
                continue
            except Exception as exc:
                dest.unlink(missing_ok=True)
                errors.append({"name": orig_name, "error": f"Не вдалося відкрити зображення ({exc})"})
                continue

            with _lock:
                sess.photos[pid] = ph
            sess.invalidate_zip()
            try:
                _make_thumb(ph, sess)
            except Exception:
                traceback.print_exc()
            accepted.append({"id": ph.id, "name": ph.name, "w": ph.w, "h": ph.h,
                             "bytes": ph.size, "state": ph.state})

        return JSONResponse({
            "session": sess.id,
            "accepted": accepted,
            "errors": errors,
        })

    # ---------------- обробка ----------------

    @app.post("/api/process")
    async def process(payload: dict) -> dict:
        sid = str(payload.get("session") or "")
        sess = _get_session(sid)
        scale = int(payload.get("scale") or 0)
        fmt = str(payload.get("format") or "")
        pids = list(payload.get("photo_ids") or [])

        if not (1 <= scale <= 8):
            raise _err("Коефіцієнт має бути в межах 1–8.")
        if fmt not in config.OUTPUT_FORMATS:
            raise _err("Невідомий формат результату.")
        if not (1 <= len(pids) <= config.MAX_PHOTOS):
            raise _err(f"Обробці підлягає від 1 до {config.MAX_PHOTOS} фото.")

        with _lock:
            sess.scale = scale
            sess.fmt = fmt
            sess.invalidate_zip()
            targets: list[Photo] = []
            for pid in pids:
                ph = sess.photos.get(pid)
                if ph is None or ph.state == "removed":
                    raise _err(f"Фото не знайдено: {pid}", 404)
                if ph.state in ("queued", "working"):
                    raise _err("Фото вже в обробці")
                if ph.state == "done":
                    raise _err("Фото вже оброблено — почніть нову сесію")
                if (ph.w * scale) * (ph.h * scale) / 1_000_000 > config.MAX_OUTPUT_MP:
                    raise _err(
                        f"Фото «{ph.name}»: результат {ph.w * scale}×{ph.h * scale} перевищує "
                        f"ліміт {config.MAX_OUTPUT_MP:.0f} Мп. Зменшіть коефіцієнт."
                    )
                ph.state = "queued"
                ph.progress = 0.0
                ph.stage = "Черга…"
                ph.error = ""
                targets.append(ph)

        for ph in targets:
            _run_photo(sess, ph, eng, runner)

        return {"ok": True, "queued": len(targets)}

    @app.get("/api/status/{sid}")
    def status(sid: str) -> dict:
        sess = _get_session(sid)
        with _lock:
            photos = [ph.to_dict(sess) for ph in list(sess.photos.values())
                      if ph.state != "removed"]
            return {
                "session": sess.id,
                "scale": sess.scale,
                "format": sess.fmt,
                "device": eng.device_label,
                "pending": any(ph.state in ("queued", "working")
                               for ph in sess.photos.values() if ph.state != "removed"),
                "photos": photos,
            }

    @app.delete("/api/photos/{sid}/{pid}")
    def delete_photo(sid: str, pid: str) -> dict:
        sess = _get_session(sid)
        with _lock:
            ph = sess.photos.get(pid)
            if ph is None:
                raise _err("Фото не знайдено", 404)
            if ph.state in ("queued", "working"):
                raise _err("Фото в обробці — неможливо видалити", 409)
            ph.state = "removed"
            sess.invalidate_zip()
        try:
            ph.path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": True}

    # ---------------- файли результатів ----------------

    @app.get("/api/photos/{sid}/{pid}/thumb")
    def thumb(sid: str, pid: str) -> FileResponse:
        sess = _get_session(sid)
        ph = _get_photo(sess, pid)
        p = sess.dir / "thumbs" / f"{ph.id}.jpg"
        if not p.exists():
            _make_thumb(ph, sess)
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/api/photos/{sid}/{pid}/orig-preview")
    def orig_preview(sid: str, pid: str) -> FileResponse:
        sess = _get_session(sid)
        ph = _get_photo(sess, pid)
        p = sess.dir / "thumbs" / f"{ph.id}_orig.jpg"
        if not p.exists():
            engine.make_preview(ph.path, p, max_side=1200, quality=85)
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/api/results/{sid}/{pid}/download")
    def download(sid: str, pid: str) -> FileResponse:
        sess = _get_session(sid)
        ph = _get_photo(sess, pid)
        if ph.state != "done" or not ph.out_path or not ph.out_path.exists():
            raise _err("Результат ще не готовий", 409)
        stem = _safe_name(Path(ph.name).stem, 60)
        fname = f"{stem}_x{sess.scale}{ph.out_path.suffix}"
        return FileResponse(ph.out_path, filename=fname,
                            media_type=_media_type(ph.out_path))

    @app.get("/api/results/{sid}/{pid}/preview")
    def result_preview(sid: str, pid: str) -> FileResponse:
        sess = _get_session(sid)
        ph = _get_photo(sess, pid)
        p = sess.dir / "out" / f"{ph.id}_preview.jpg"
        if not p.exists():
            if ph.state != "done" or not ph.out_path:
                raise _err("Результат ще не готовий", 409)
            engine.make_preview(ph.out_path, p, max_side=1200, quality=85)
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/api/results/{sid}/zip")
    async def zip_all(sid: str) -> FileResponse:
        sess = _get_session(sid)
        zpath = await asyncio.to_thread(_build_zip, sess)
        if zpath is None:
            raise _err("Немає готових результатів для архіву", 409)
        return FileResponse(zpath, filename=f"upscaled_x{sess.scale}.zip",
                            media_type="application/zip")

    # ---------------- помилки ----------------

    @app.exception_handler(UserError)
    async def user_error(_, exc: UserError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unexpected(_, exc: Exception) -> JSONResponse:
        traceback.print_exc()
        return JSONResponse(status_code=500,
                            content={"detail": "Внутрішня помилка сервера — див. консоль програми."})

    return app


def _media_type(path: Path) -> str:
    import mimetypes
    mt, _ = mimetypes.guess_type(path.name)
    return mt or "application/octet-stream"

"""Обчислювальний рушій апскейлу Photo Upscale Studio.

Підхід: нейромережева супререшітка **Real-ESRGAN** (RRDBNet) у форматі
ONNX, що виконується через **onnxruntime**:
 * Windows: провайдер DirectML — працює з GPU NVIDIA/AMD/Intel та
   не блокується Smart App Control (компоненти підписані Microsoft);
 * інші ОС: CPUExecutionProvider (та CUDA, якщо встановлено ort-gpu).

Ваги: офіційні realesrgan-x4plus та RealESRGAN_x2plus (BSD-3-Clause,
xinntao/Real-ESRGAN). x4-експорт має опубліковані SHA-256, тож файли
перевіряються при завантаженні; x2 — відомий репозиторій конверсій,
додатково верифікований порівняльним тестом (кореляція 0.995 з
даунскейлом офіційної x4-моделі).

На відміну від бікубічних інтерполяцій, Real-ESRGAN синтезує текстури
та зберігає різкість країв замість «розмазування» пікселів.

Технічно:
 * ваги кешуються у ``models/`` (завантаження автоматичне при першому
   фото, з прогресом та SHA-перевіркою);
 * великі зображення обробляються **тайлами фіксованої форми** з
   перекриттям: єдина форма тайла означає одну компіляцію шейдерів у
   DirectML і передбачуване споживання пам'яті незалежно від розміру
   фото; у результат іде лише «ядро» тайла — швів не видно;
 * масштаб 8× = ланцюжок 4× → 2× з офіційних моделей; проміжні
   коефіцієнти (3×, 5×, 6×, 7×) досягаються фінальним Lanczos-ресайзом.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import onnxruntime as ort
import requests
from PIL import Image, ImageOps

from . import config

Image.MAX_IMAGE_PIXELS = None  # свої великі файли не є «бомбами»

PHASE_PREPARE = "prepare"
PHASE_DOWNLOAD = "download"
PHASE_UPSCALE = "upscale"
PHASE_FINALIZE = "finalize"

# сигнатура: (фаза, відсоток 0..100, текст статусу)
ProgressCb = Callable[[str, float, str], None]


class UserError(RuntimeError):
    """Помилка, яку коректно показуємо користувачу українською."""


# --------------------------------------------------------------------------
# Пристрій виконання
# --------------------------------------------------------------------------

def _available_providers() -> list[str]:
    try:
        return list(ort.get_available_providers())
    except Exception:
        return ["CPUExecutionProvider"]


# --------------------------------------------------------------------------
# Ланцюжок моделей для цільового коефіцієнта
# --------------------------------------------------------------------------

def chain_for(target_scale: int) -> tuple[str, ...]:
    """Мінімальний набір моделей (4× / 2×), що дає масштаб >= таргету.

    2× → [x2]; 3–4× → [x4]; 5–8× → [x4, x2]; 1× → [] (без апскейлу).
    """
    if target_scale <= 1:
        return ()
    best: tuple[int, int, int] | None = None   # (count_x4, count_x2, product)
    for a in range(0, 3):
        for b in range(0, 3):
            product = (4 ** a) * (2 ** b)
            if product < target_scale:
                continue
            cand = (a, b, product)
            if best is None or (product, a + b) < (best[2], best[0] + best[1]):
                best = cand
    assert best is not None
    return ("x4",) * best[0] + ("x2",) * best[1]


# --------------------------------------------------------------------------
# Завантаження та перевірка вагів
# --------------------------------------------------------------------------

def _download_file(url: str, dest: Path, sha256_hex: str | None,
                   progress: ProgressCb) -> None:
    import hashlib
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    headers = {"User-Agent": "photo-upscale-studio/1.0"}
    last_err: Exception | None = None

    for attempt in range(1, config.DOWNLOAD_RETRIES + 1):
        try:
            with requests.get(url, stream=True, timeout=config.DOWNLOAD_TIMEOUT,
                              headers=headers) as r:
                r.raise_for_status()
                digest = hashlib.sha256()
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = r.raw.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        digest.update(chunk)
                        if total:
                            progress(PHASE_DOWNLOAD, done * 100.0 / total,
                                     f"завантажено {done // (1024 * 1024)} МБ "
                                     f"з {max(total // (1024 * 1024), 1)}")
            if sha256_hex and digest.hexdigest().lower() != sha256_hex.lower():
                tmp.unlink(missing_ok=True)
                raise UserError("Файл моделі пошкоджений при завантаженні "
                                "(SHA-256 не збігся). Спробуйте ще раз.")
            tmp.rename(dest)
            return
        except UserError:
            raise
        except Exception as exc:
            last_err = exc
            progress(PHASE_DOWNLOAD, 0,
                     f"Не вдалося завантажити — спроба {attempt}/{config.DOWNLOAD_RETRIES}")
            time.sleep(2)

    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    raise UserError(
        f"Не вдалося завантажити модель ({last_err}). "
        "Перевірте з'єднання та спробуйте ще раз."
    )


# --------------------------------------------------------------------------
# Тайлова інференс
# --------------------------------------------------------------------------

def _tiled_inference(
    canvas_in: np.ndarray,          # (H, W, 3) uint8 — джерело етапу
    sess: "ort.InferenceSession",
    in_name: str,
    out_name: str,
    model_scale: int,
    tile: int,
    overlap: int,
    note: Callable[[float, str], None],
) -> np.ndarray:
    """Інференс фіксованими тайлами tile x tile (форма входу постійна!).

    «Ядро» (core) розміром tile-2*overlap — область, чий результат
    записуємо у вихід; довколишні `overlap` пікселів служать мережі
    контекстом. Сусідні ядра стуляються без проміжків, тож швів не
    виникає. Контекст біля країв фото доповнюється падом «edge»
    (копіювання крайніх рядків/стовпців) до рівно tile x tile.
    """
    H, W = canvas_in.shape[:2]
    core = tile - 2 * overlap
    Hp = -(-H // core) * core          # вирівнюємо під ціле число ядер
    Wp = -(-W // core) * core
    if Hp != H or Wp != W:
        src = np.pad(canvas_in, ((0, Hp - H), (0, Wp - W), (0, 0)), mode="edge")
    else:
        src = canvas_in

    out = np.empty((H * model_scale, W * model_scale, 3), dtype=np.uint8)
    s = model_scale

    total = (Hp // core) * (Wp // core)
    idx = 0
    for y0 in range(0, Hp, core):
        cy0 = max(0, y0 - overlap)
        cy1 = min(Hp, y0 + core + overlap)
        for x0 in range(0, Wp, core):
            cx0 = max(0, x0 - overlap)
            cx1 = min(Wp, x0 + core + overlap)

            tile_in = src[cy0:cy1, cx0:cx1]
            # контексту бракує лише біля країв → дозаповнюємо падом до tile
            ph = tile - tile_in.shape[0]
            pw = tile - tile_in.shape[1]
            if ph or pw:
                tile_in = np.pad(tile_in,
                                 ((ph // 2, ph - ph // 2),
                                  (pw // 2, pw - pw // 2), (0, 0)), mode="edge")

            x_t = np.ascontiguousarray(tile_in).transpose(2, 0, 1)[None] \
                .astype(np.float32) / 255.0
            o = sess.run([out_name], {in_name: x_t})[0][0]     # (3, T*s, T*s)
            o = np.clip(o * 255.0, 0, 255).round().astype("uint8") \
                .transpose(1, 2, 0)

            # «ядро» у виході моделі → відповідна область у результаті
            ky = (ph // 2 + (y0 - cy0)) * s
            kx = (pw // 2 + (x0 - cx0)) * s
            oy1, ox1 = min(y0 + core, H) * s, min(x0 + core, W) * s
            if oy1 > y0 * s and ox1 > x0 * s:
                h_out, w_out = oy1 - y0 * s, ox1 - x0 * s
                out[y0 * s:oy1, x0 * s:ox1] = o[ky:ky + h_out,
                                                kx:kx + w_out]

            idx += 1
            if idx != total:
                note(idx * 100.0 / total, f"тайл {idx}/{total}")
    note(100.0, f"тайлів: {total}")
    return out


# --------------------------------------------------------------------------
# Головний рушій
# --------------------------------------------------------------------------

class Engine:
    """Провайдер (DirectML/CUDA/CPU) + керування ONNX-сесіями + пайплайн."""

    def __init__(self) -> None:
        config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        provs = _available_providers()
        self.dml_possible = "DmlExecutionProvider" in provs
        if self.dml_possible:
            self.provider = "dml"
            self.device_label = "GPU · DirectML"
        elif "CUDAExecutionProvider" in provs:
            self.provider = "cuda"
            self.device_label = "GPU · CUDA"
        else:
            self.provider = "cpu"
            self.device_label = f"CPU ({os.cpu_count() or 4} потоків)"
        self.dml_dev: int | None = None              # обраний адаптер
        self.tile = config.DEVICE_TILE.get(self.provider, 384)
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------- сесії -------------------------

    def _session_options(self) -> "ort.SessionOptions":
        so = ort.SessionOptions()
        so.log_severity_level = 3
        if self.provider == "cpu":
            so.intra_op_num_threads = max(2, min(8, os.cpu_count() or 4))
        return so

    def _providers_for(self) -> list:
        if self.provider == "dml":
            if self.dml_dev is None:
                return ["DmlExecutionProvider"]
            return [("DmlExecutionProvider", {"device_id": self.dml_dev})]
        if self.provider == "cuda":
            return ["CUDAExecutionProvider"]
        return ["CPUExecutionProvider"]

    def _bench_dml(self, path: Path) -> tuple[int, float] | None:
        """Обирає найшвидший *реально доступний* адаптер DirectML (id 0/1)."""
        best: tuple[int, float] | None = None
        for dev in (0, 1):
            try:
                so = ort.SessionOptions()
                so.log_severity_level = 3
                s = ort.InferenceSession(
                    str(path), so,
                    providers=[("DmlExecutionProvider", {"device_id": dev})])
                # ORT при невалідному device_id мовчки будує CPU-сесію —
                # тому обов'язково перевіряємо, який провайдер вийшов.
                if "DmlExecutionProvider" not in s.get_providers():
                    continue
                name = s.get_inputs()[0].name
                x = np.zeros((1, 3, 128, 128), np.float32)
                s.run(None, {name: x})                 # компіляція шейдерів
                t0 = time.time()
                s.run(None, {name: x})
                dt = time.time() - t0
                if best is None or dt < best[1]:
                    best = (dev, dt)
            except Exception:
                continue
        return best

    def _switch_to_cpu(self) -> None:
        self.provider = "cpu"
        self.device_label = f"CPU ({os.cpu_count() or 4} потоків)"
        self.tile = config.DEVICE_TILE.get("cpu", 384)
        self._sessions.clear()

    def get_session(self, key: str, progress: ProgressCb
                    ) -> tuple["ort.InferenceSession", str, str, int]:
        """Повертає (session, in_name, out_name, scale); ваги за потреби."""
        with self._lock:
            cached = self._sessions.get(key)
            if cached is not None:
                return (cached["sess"], cached["in"], cached["out"], cached["scale"])

            spec = config.MODEL_SPECS[key]
            variant = "gpu" if self.provider in ("dml", "cuda") else "cpu"
            fname = spec["files"][variant]
            meta = spec["files_meta"][fname]
            path = config.MODELS_DIR / fname
            if not path.exists():
                _download_file(meta["url"], path, meta.get("sha256"), progress)
            progress(PHASE_DOWNLOAD, 100, f"{spec['title']} · збірка сесії…")

            # пряма перевірка адаптера DirectML на першій сесії
            if self.provider == "dml" and self.dml_dev is None:
                pick = self._bench_dml(path)
                if pick is not None:
                    self.dml_dev = pick[0]
                else:
                    self._switch_to_cpu()
                    variant = "cpu"
                    fname = spec["files"]["cpu"]
                    meta = spec["files_meta"][fname]
                    path = config.MODELS_DIR / fname
                    if not path.exists():
                        _download_file(meta["url"], path, meta.get("sha256"), progress)
                    progress(PHASE_DOWNLOAD, 100, f"{spec['title']} · збірка сесії…")

            try:
                sess = ort.InferenceSession(str(path), self._session_options(),
                                            providers=self._providers_for())
                if (self.provider == "dml"
                        and "DmlExecutionProvider" not in sess.get_providers()):
                    # ORT мовчки підмінив DML-сесію CPU — перемикаємось явно
                    raise RuntimeError("сесія DirectML не активувалась")
            except Exception as exc:
                if self.provider == "dml":
                    self._switch_to_cpu()
                    variant = "cpu"
                    fname = spec["files"]["cpu"]
                    meta = spec["files_meta"][fname]
                    path = config.MODELS_DIR / fname
                    if not path.exists():
                        _download_file(meta["url"], path, meta.get("sha256"),
                                       progress)
                    progress(PHASE_DOWNLOAD, 100,
                             f"{spec['title']} · CPU-збірка сесії…")
                    try:
                        sess = ort.InferenceSession(
                            str(path), self._session_options(),
                            providers=self._providers_for())
                    except Exception as exc2:
                        raise UserError("Не вдалося підготувати модель "
                                        f"апскейлу: {exc2}") from exc2
                else:
                    raise UserError(
                        f"Не вдалося підготувати модель апскейлу: {exc}"
                    ) from exc

            in_name = sess.get_inputs()[0].name
            out_name = sess.get_outputs()[0].name
            self._sessions[key] = {
                "sess": sess, "in": in_name, "out": out_name, "scale": spec["scale"],
            }
            if self.provider == "dml":
                self.device_label = f"GPU · DirectML (адаптер {self.dml_dev})"
            return sess, in_name, out_name, spec["scale"]

    # ------------------------- пайплайн -------------------------

    def upscale(
        self,
        input_path: Path,
        output_path: Path,
        scale: int,
        fmt: str,
        progress: ProgressCb,
    ) -> dict:
        """Апскейл одного фото. Повертає метадані результату."""
        started = time.time()
        progress(PHASE_PREPARE, 0, "Читання зображення…")

        try:
            with Image.open(input_path) as im:
                im = ImageOps.exif_transpose(im)
                has_alpha = im.mode in ("RGBA", "LA") or (
                    im.mode == "P" and "transparency" in im.info)
                if has_alpha:
                    rgba = im.convert("RGBA")
                    alpha = rgba.getchannel("A")
                    rgb = rgba.convert("RGB")
                else:
                    alpha = None
                    rgb = im.convert("RGB")
        except UserError:
            raise
        except Exception as exc:
            raise UserError(
                "Не вдалося прочитати зображення (файл пошкоджений або "
                f"не є фото JPG/PNG/WEBP): {exc}"
            ) from exc

        w, h = rgb.size
        if min(w, h) < config.MIN_IMAGE_DIM:
            raise UserError("Зображення надто мале (менше 4 пікселів по стороні).")

        out_w, out_h = w * scale, h * scale
        mp = out_w * out_h / 1_000_000
        if mp > config.MAX_OUTPUT_MP:
            raise UserError(
                f"Результат {out_w}×{out_h} (~{mp:.0f} Мп) перевищує безпечний "
                f"ліміт {config.MAX_OUTPUT_MP:.0f} Мп. Зменшіть коефіцієнт."
            )

        canvas: np.ndarray = np.asarray(rgb, dtype=np.uint8)

        chain = chain_for(scale)
        n = len(chain)
        for i, key in enumerate(chain):
            spec = config.MODEL_SPECS[key]

            def dl_cb(_phase: str, pct: float, note_txt: str = "",
                      _sp=spec) -> None:
                progress(PHASE_DOWNLOAD, 3 + min(pct, 100.0) * 0.05,
                         f"{_sp['title']} · {note_txt or 'завантаження…'}")

            sess, in_name, out_name, mscale = self.get_session(key, dl_cb)

            def note(jpct: float, txt: str, _i=i, _m: int = mscale) -> None:
                progress(PHASE_UPSCALE, 8 + (_i + jpct / 100.0) * 87.0 / n,
                         f"Модель {_m}× · {txt}")

            canvas = _tiled_inference(canvas, sess, in_name, out_name, mscale,
                                      self.tile, config.TILE_OVERLAP, note)

        progress(PHASE_FINALIZE, 95, "Фінальна обробка та збереження…")

        if canvas.shape[1] != out_w or canvas.shape[0] != out_h:
            img = Image.fromarray(canvas).resize((out_w, out_h), Image.LANCZOS)
        else:
            img = Image.fromarray(canvas)

        if alpha is not None:
            img = img.convert("RGB")
            img.putalpha(alpha.resize((out_w, out_h), Image.BICUBIC))

        _save(img, output_path, fmt)
        progress(PHASE_FINALIZE, 100, "Готово")
        return {
            "orig_w": w, "orig_h": h,
            "out_w": out_w, "out_h": out_h,
            "seconds": round(time.time() - started, 1),
        }


# --------------------------------------------------------------------------
# Збереження
# --------------------------------------------------------------------------

def _flatten(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    bg = Image.new("RGB", img.size, (255, 255, 255))
    mask = img.getchannel("A") if "A" in img.getbands() else None
    bg.paste(img, mask=mask)
    return bg


def _save(img: Image.Image, path: Path, fmt: str) -> None:
    try:
        if fmt == "png":
            img.save(path, format="PNG", compress_level=5)
        elif fmt == "jpeg":
            _flatten(img).save(path, format="JPEG",
                               quality=config.OUTPUT_QUALITY["jpeg"],
                               subsampling=0, optimize=False)
        elif fmt == "webp":
            img.save(path, format="WEBP", quality=config.OUTPUT_QUALITY["webp"],
                     method=4)
        else:  # pragma: no cover
            raise UserError(f"Невідомий формат результату: {fmt}")
    except UserError:
        raise
    except MemoryError as exc:
        raise MemoryError("замало пам'яті для запису результату") from exc
    except Exception as exc:
        raise UserError(f"Не вдалося зберегти результат: {exc}") from exc


def make_preview(src: Path, dst: Path, max_side: int = 1440, quality: int = 88) -> None:
    """Зменшена копія для показу в браузері (щоб не вантажити 200-Мп файл)."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        _flatten(im).save(dst, format="JPEG", quality=quality)

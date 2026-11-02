"""Самоперевірка Photo Upscale Studio (після запуску run.py).

    python scripts/selftest.py [PORT]

Тестує: ліміт 5 фото, невалідний файл, обробку (JPG+WEBP), помилку по
одному фото ( решта обробляються ), розміри результатів, 8×-ланцюжок,
завантаження та ZIP. Працює лише по локальному HTTP — безпечний.
"""
from __future__ import annotations

import io
import sys
import tempfile
import time
import zipfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import requests
from PIL import Image, ImageDraw

BASE = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else 8560}"
TMP = Path(tempfile.mkdtemp(prefix="pus_selftest_"))

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✔ " if cond else "  ✘ НЕ ПРОЙДЕНО: ") + label)
    if not cond:
        FAILED.append(label)


def gen_photo(name: str, w: int, h: int = None, fmt: str = "jpg") -> Path:
    """Створює тестове «фото»: градієнт + шум + круги + текст."""
    h = h or w
    rng = np.random.default_rng(42)
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.stack([
        yy / max(h - 1, 1) * 255,
        (xx / max(w - 1, 1)) * 200 + 30,
        np.full((h, w), 160),
    ], axis=2)
    noise = rng.normal(0, 14, (h, w, 3))
    arr = np.clip(base + noise, 0, 255).astype("uint8")
    img = Image.fromarray(arr)
    d = ImageDraw.Draw(img)
    d.ellipse((w // 2 - 40, h // 2 - 40, w // 2 + 40, h // 2 + 40), outline=(255, 240, 10), width=3)
    d.rectangle((20, 20, 90, 70), outline=(255, 90, 200), width=3)
    d.text((110, 110), f"{w}x{h} detail test", fill=(255, 255, 255))
    p = TMP / name
    if fmt == "jpg":
        img.save(p, "JPEG", quality=92)
    elif fmt == "png":
        img.save(p, "PNG")
    else:
        img.save(p, fmt.upper())
    return p


def upload(paths: list[Path], session: str | None = None) -> dict:
    files = [("files", (p.name, open(p, "rb"),
                        {"jpg": "image/jpeg"}.get(p.suffix.strip("."), "image/" + p.suffix.strip("."))))
             for p in paths]
    data = {}
    if session:
        data["session"] = session
    r = requests.post(f"{BASE}/api/upload", files=files, data=data, timeout=120)
    return r


def wait_processing(sid: str, scale: int, fmt: str, pids: list[str], timeout_s: int = 1800) -> dict:
    r = requests.post(f"{BASE}/api/process", json={
        "session": sid, "photo_ids": pids, "scale": scale, "format": fmt}, timeout=30)
    r.raise_for_status()
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = requests.get(f"{BASE}/api/status/{sid}", timeout=30).json()
        pending = st["pending"]
        if not pending:
            return st
        for ph in st["photos"]:
            if ph["state"] in ("working", "queued"):
                print(f"    … {ph['name']}: {ph.get('stage','')} {ph.get('progress',0)}%"
                      + (f" ETA {ph['eta']}с" if ph.get("eta") else ""))
        time.sleep(5)
    raise TimeoutError("обробка триває дуже довго")


def main() -> None:
    print("BASE:", BASE)
    print("\n[1] /api/info")
    info = requests.get(f"{BASE}/api/info", timeout=15).json()
    check(info.get("max_photos") == 5, "info.max_photos == 5")
    print("    пристрій:", info["device"])

    print("\n[2] ліміт 5 фото за раз")
    six = [gen_photo(f"lot_{i}.jpg", 160) for i in range(6)]
    resp = upload(six)
    check(resp.status_code == 400 and "Максимум" in resp.json().get("detail", ""),
          "6 файлів → відхилено з зрозумілим повідомленням")

    print("\n[3] сесія: валідні + пошкоджений файл + WEBP")
    p_jpg = gen_photo("good1.jpg", 340, 240, "jpg")
    p_bad = TMP / "broken.jpg"
    p_bad.write_bytes(b"not an image at all")
    p_webp = gen_photo("good2.webp", 300, 220, "webp")

    r = upload([p_jpg, p_bad, p_webp])
    check(r.status_code == 200, "завантаження 3 файлів ок")
    data = r.json()
    check(len(data["accepted"]) == 2, "прийнято 2 валідні")
    check(len(data["errors"]) == 1 and "зображення" in data["errors"][0]["error"],
          "пошкоджений файл → зрозуміла помилка")
    sid = data["session"]
    pids = [p["id"] for p in data["accepted"]]

    print("\n[4] друге завантаження до тієї ж сесії (+ ще 2 фото)")
    r2 = upload([gen_photo("good3.jpg", 250, 200), gen_photo("good4.png", 260, 200, "png")], session=sid)
    check(r2.status_code == 200 and len(r2.json()["accepted"]) == 2, "додано ще 2")
    pids_all = pids + [p["id"] for p in r2.json()["accepted"]]

    print("\n[5] обробка масштабу 2× PNG (валідні) + пошкоджений окремо")
    st = wait_processing(sid, 2, "png", pids_all)
    states = {p["id"]: p for p in st["photos"]}
    ok = 0
    for pid, ph in states.items():
        if ph["state"] == "done":
            ok += 1
            d = requests.get(f"{BASE}/api/results/{sid}/{pid}/download", timeout=120)
            img = Image.open(io.BytesIO(d.content))
            img.load()
            check(img.size == (ph["w"] * 2, ph["h"] * 2),
                  f"[{ph['name']}] розмір {img.size} == {ph['w']*2}x{ph['h']*2}")
    for pid, ph in states.items():
        if ph["state"] == "error":
            check("не є" in ph.get("error", "") or "пошкодж" in ph.get("error", ""),
                  f"[{ph['name']}] помилка показана: {ph.get('error')}")
    check(ok == 4, f"оброблено {ok}/4 валідних, помилка не заважає")

    print("\n[6] ZIP-архів")
    z = requests.get(f"{BASE}/api/results/{sid}/zip", timeout=300)
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    check(len(zf.namelist()) == 4, f"у ZIP {len(zf.namelist())} файлів")

    print("\n[7] 8×-ланцюжок на маленькому фото (x4 → x2)")
    p_top = gen_photo("scale8.jpg", 232, 152, "jpg")
    r = upload([p_top])
    sid2 = r.json()["session"]
    pid2 = r.json()["accepted"][0]["id"]
    st2 = wait_processing(sid2, 8, "jpeg", [pid2], timeout_s=3600)
    ph = st2["photos"][0]
    check(ph["state"] == "done", "8× успішно")
    if ph["state"] == "done":
        d = requests.get(f"{BASE}/api/results/{sid2}/{pid2}/download", timeout=120)
        img = Image.open(io.BytesIO(d.content))
        check(img.size == (232 * 8, 152 * 8), f"8× розмір {img.size}")
        print("    час 8×:", ph["elapsed"], "с на", info["device"])

    print()
    if FAILED:
        print("НЕ ПРОЙДЕНО:", len(FAILED))
        for lbl in FAILED:
            print("  ✘", lbl)
        sys.exit(1)
    print("УСІ ПЕРЕВІРКИ ПРОЙДЕНО ✔")


if __name__ == "__main__":
    main()

/* Photo Upscale Studio — клієнтська логіка (без білд-кроку, чистий JS) */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);

const state = {
  sessionId: null,
  photos: new Map(),      // id → {id, name, w, h, bytes}
  maxPhotos: 5,
  maxOutputMp: 256,
  scale: 8,
  format: "jpeg",
  processing: false,
  resultsShown: new Set(), // id фото, для яких вже намальовано картку результату
  pollTimer: null,
};

const AllowedExt = new Set(["jpg", "jpeg", "png", "webp"]);

/* ---------------- утиліти ---------------- */

function fmtBytes(n) {
  if (n == null || isNaN(n)) return "—";
  if (n < 1024) return n + " Б";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " КБ";
  return (n / 1024 / 1024).toFixed(1) + " МБ";
}

function fmtDims(w, h) { return `${w}×${h}`; }

function showErrors(list) {
  const box = $("#errorBox");
  if (!list || !list.length) { box.classList.add("hidden"); return; }
  box.textContent = list.join("\n");
  box.classList.remove("hidden");
}

function pushError(msg) {
  const box = $("#errorBox");
  const lines = box.classList.contains("hidden") ? [] : box.textContent.split("\n");
  if (!lines.includes(msg)) lines.push(msg);
  showErrors(lines);
}

/* ---------------- ініціалізація ---------------- */

async function loadInfo() {
  try {
    const info = await (await fetch("/api/info")).json();
    state.maxPhotos = info.max_photos;
    state.maxOutputMp = info.max_output_mp;
    $("#deviceBadge").textContent = info.device;
    $("#deviceBadge").title = info.gpu
      ? "Апскейл виконується на відеокарті — швидко"
      : "Апскейл на процесорі — повільніше. Показ, як увімкнути GPU, є в README";
    document.querySelectorAll(".max-photos").forEach(el => el.textContent = info.max_photos);
  } catch {
    $("#deviceBadge").textContent = "сервер недоступний";
  }
}

// після вибору пристрою бейдж у шапці оновлюється наступним опитуванням
function refreshDeviceBadge() {
  setTimeout(loadInfo, 500);
}

/* ---------------- завантаження файлів ---------------- */

function isAllowed(file) {
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  return AllowedExt.has(ext);
}

async function addFiles(fileList) {
  if (state.processing) return;
  const files = Array.from(fileList || []);

  const bad = files.filter(f => !isAllowed(f));
  const good = files.filter(isAllowed);

  const free = state.maxPhotos - state.photos.size;
  const taken = good.slice(0, Math.max(0, free));
  const overflow = good.slice(taken.length);

  const errs = [];
  for (const f of bad) errs.push(`«${f.name}» — формат не підтримується (потрібні JPG, PNG, WEBP)`);
  if (overflow.length) {
    errs.push(
      `Максимум ${state.maxPhotos} фото за раз — зайві не завантажено (${overflow.length} шт.: ` +
      overflow.slice(0, 3).map(f => f.name).join(", ") + (overflow.length > 3 ? "…" : "") + `)`
    );
  }
  showErrors(errs.length ? errs : null);

  for (const f of taken) await uploadOne(f);
  refreshEditor();
}

function cardEl(pid, name) {
  // картка у галереї мініатюр
  const tpl = $("#tplUploadCard");
  const node = tpl.content.firstElementChild.cloneNode(true);
  node.dataset.pid = pid;
  $(".thumb-name", node).textContent = name;
  const btn = $(".thumb-remove", node);
  btn.addEventListener("click", () => removePhoto(pid, node));
  return node;
}

async function uploadOne(file) {
  const pid = "tmp_" + Math.random().toString(36).slice(2, 8);
  const node = cardEl(pid, file.name);
  $(".thumb-meta", node).textContent = "завантаження…";
  $("#gallery").appendChild(node);

  // локальна мініатюра одразу (поки сервер готує свою)
  try {
    const url = URL.createObjectURL(file);
    const img = document.createElement("img");
    img.onload = () => { $(".thumb-media", node).innerHTML = ""; img.src = url; };
    img.src = url;
  } catch {}

  const fd = new FormData();
  fd.append("files", file);          // сервер очікує поле `files` (list[UploadFile])
  if (state.sessionId) fd.append("session", state.sessionId);

  try {
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      node.remove();
      const det = typeof data.detail === "string"
        ? data.detail
        : "сервер відхилив файл (невірний запит)";
      pushError(`${file.name}: ${det}`);
      return;
    }
    if (data.errors && data.errors.length) {
      node.remove();
      pushError(`${file.name}: ${data.errors[0].error}`);
      return;
    }
    state.sessionId = data.session;
    const ph = data.accepted[0];
    state.photos.set(ph.id, ph);
    node.dataset.pid = ph.id;
    $(".thumb-media", node).innerHTML = `<img src="/api/photos/${data.session}/${ph.id}/thumb" alt="">`;
    $(".thumb-meta", node).textContent = `${fmtDims(ph.w, ph.h)} · ${fmtBytes(ph.bytes)}`;
  } catch (e) {
    node.remove();
    pushError(`${file.name}: сервер недоступний (${e})`);
  }
}

/* ---------------- видалення ---------------- */

async function removePhoto(pid, node) {
  node.remove();
  if (!state.sessionId || pid.startsWith("tmp_")) { state.photos.delete(pid); refreshEditor(); return; }
  const real = state.photos.get(pid);
  if (!real) { refreshEditor(); return; }
  state.photos.delete(pid);
  refreshEditor();
  try {
    const res = await fetch(`/api/photos/${state.sessionId}/${pid}`, { method: "DELETE" });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      if (res.status !== 404) pushError(d.detail || "не вдалося видалити файл");
    }
  } catch {}
}

/* ---------------- налаштування перед стартом ---------------- */

function biggestPhoto() {
  let best = null;
  state.photos.forEach(p => { if (!best || p.w * p.h > best.w * best.h) best = p; });
  return best;
}

function updateScaleUI() {
  state.scale = parseInt($("#scaleSlide").value, 10);
  $("#scaleVal").textContent = state.scale + "×";

  const best = biggestPhoto();
  const startBtn = $("#startBtn");
  if (!best) {
    $("#scaleHint").textContent = "";
    startBtn.disabled = true;
    return;
  }
  const mp = (best.w * state.scale) * (best.h * state.scale) / 1e6;
  const out = fmtDims(best.w * state.scale, best.h * state.scale);
  const txt = `Найбільше фото → ${out} (~${mp.toFixed(1)} Мп · ліміт ${state.maxOutputMp} Мп)`;
  const over = mp > state.maxOutputMp;
  $("#scaleHint").innerHTML = over
    ? `<span class="over">${txt} — зменшіть коефіцієнт</span>`
    : txt;
  startBtn.disabled = over;
  return;
}

function refreshEditor() {
  const empty = state.photos.size === 0;
  $("#editorPanel").classList.toggle("hidden", empty);
  $("#counter").textContent = empty ? "" :
    `${state.photos.size} з ${state.maxPhotos} завантажено`;
  state.processing ? null : updateScaleUI();
}

/* ---------------- запуск обробки ---------------- */

async function startProcess() {
  if (state.processing || state.photos.size === 0) return;
  state.format = $("#fmtSel").value;
  state.device = $("#devSel").value;
  try { localStorage.setItem("pusDevice", state.device); } catch (_) {}

  const res = await fetch("/api/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session: state.sessionId,
      photo_ids: Array.from(state.photos.keys()),
      scale: state.scale,
      format: state.format,
      device: state.device,
    }),
  }).catch(e => ({ ok: false, detail: "" + e }));

  if (!res.ok) {
    const d = res.detail !== undefined ? res.detail : "не вдалося запустити обробку";
    const data = await res.json?.().catch(() => ({}));
    pushError(data.detail || d);
    return;
  }

  refreshDeviceBadge();

  state.processing = true;
  $("#dropzone").classList.add("hidden");
  $("#startBtn").disabled = true;
  $("#progressPanel").classList.remove("hidden");
  $("#resetBtn").classList.remove("hidden");

  const ids = Array.from(state.photos.keys());
  $("#progressList").innerHTML = "";
  for (const pid of ids) {
    const ph = state.photos.get(pid);
    const card = document.createElement("div");
    card.className = "prog-card";
    card.dataset.pid = pid;
    card.innerHTML = `
      <div class="prog-top">
        <span class="prog-name">${escapeHtml(ph.name)}</span>
        <span class="badge">черга</span>
      </div>
      <div class="bar"><i></i></div>
      <div class="prog-stage muted">очікує своєї черги…</div>`;
    $("#progressList").appendChild(card);
    ph._card = card;
  }

  pollStatus();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function pollStatus() {
  clearTimeout(state.pollTimer);
  let keep = true;
  try {
    const res = await fetch(`/api/status/${state.sessionId}`);
    if (res.ok) {
      const data = await res.json();
      for (const p of data.photos) {
        const ph = state.photos.get(p.id);
        const card = ph && ph._card;
        if (card) renderProgCard(card, p);
        if (p.state === "done") showResult(p);
      }
      const sum = $("#progressSummary");
      const total = data.photos.length;
      const done = data.photos.filter(p => p.state === "done").length;
      const errs = data.photos.filter(p => p.state === "error").length;
      const work = total - done - errs;
      sum.textContent = work ? `готово ${done} · обробляється ${work} · помилок ${errs}`
                             : `готово ${done} · помилок ${errs}`;

      const more = data.photos.length < state.photos.size;
      keep = work > 0 || more;
      if (done > 0) $("#resultsPanel").classList.remove("hidden");
    }
  } catch { /* мережеві коливання ігноруємо, наступний тік повторить */ }

  if (keep) state.pollTimer = setTimeout(pollStatus, 800);
  else finishProcessing();
}

function finishProcessing() {
  state.processing = false;
  $("#resetBtn").classList.add("visible");
}

function renderProgCard(card, p) {
  const badge = $(".badge", card);
  const bar = $(".bar > i", card);
  const stage = $(".prog-stage", card);

  badge.className = "badge " + p.state;
  if (p.state === "queued") {
    badge.textContent = "черга";
    bar.style.width = "0";
    stage.textContent = "очікує своєї черги…";
  } else if (p.state === "working") {
    badge.textContent = p.progress + "%";
    bar.style.width = p.progress + "%";
    const eta = p.eta != null ? ` · залишилось ≈ ${renderRem(p.eta)}` : "";
    stage.textContent = (p.stage || "обробка…") + eta;
  } else if (p.state === "done") {
    badge.textContent = "готово";
    bar.style.width = "100%";
    stage.textContent = `час: ${p.elapsed} с · результат ${fmtDims(p.out_w, p.out_h)} · ${fmtBytes(p.out_bytes)}`;
  } else if (p.state === "error") {
    badge.textContent = "помилка";
    bar.style.width = "0";
    stage.remove();
    const e = document.createElement("div");
    e.className = "prog-error";
    e.textContent = p.error || "невідома помилка";
    card.appendChild(e);
  }
}

function renderRem(sec) {
  sec = Math.max(0, sec | 0);
  if (sec < 90) return sec + " с";
  return Math.round(sec / 60) + " хв";
}

/* ---------------- результати ---------------- */

function showResult(p) {
  if (state.resultsShown.has(p.id)) return;
  state.resultsShown.add(p.id);

  const ph = state.photos.get(p.id);
  const name = ph ? ph.name : "фото";
  const sid = state.sessionId;

  const card = document.createElement("div");
  card.className = "result-card";
  card.innerHTML = `
    <div class="result-head">
      <span class="result-name">${escapeHtml(name)}</span>
      <span class="result-meta">до ${fmtDims(p.w, p.h)} → після ${fmtDims(p.out_w, p.out_h)} · ${fmtBytes(p.out_bytes)}</span>
    </div>
    <div class="compare">
      <img class="c-after" alt="після" src="/api/results/${sid}/${p.id}/preview">
      <img class="c-before" alt="до" src="/api/photos/${sid}/${p.id}/orig-preview">
      <span class="c-tag before">до</span><span class="c-tag after">після</span>
      <div class="c-handle" style="left:50%"></div>
    </div>
    <div class="result-actions">
      <a class="btn primary" href="/api/results/${sid}/${p.id}/download" download>⬇ Завантажити</a>
    </div>`;

  const compare = $(".compare", card);
  const ratio = (p.w / p.h) * 100;
  const setRatio = () => {
    const side = Math.min(ratio, 175);       // захист від екстремальних співвідношень
    compare.style.setProperty("--pt", side + "%");
    compare.style.paddingTop = side + "%";
    compare.style.height = "auto";
  };
  setRatio();

  let pos = 50;
  const handle = $(".c-handle", card);
  const move = (clientX) => {
    const r = compare.getBoundingClientRect();
    pos = Math.min(100, Math.max(0, ((clientX - r.left) / r.width) * 100));
    compare.style.setProperty("--pos", pos + "%");
    handle.style.left = pos + "%";
  };
  compare.addEventListener("pointermove", (e) => { if (e.buttons || e.pointerType === "touch") move(e.clientX); });
  compare.addEventListener("pointerdown", (e) => { move(e.clientX); compare.setPointerCapture(e.pointerId); });

  $("#resultsList").appendChild(card);

  const doneCount = state.resultsShown.size;
  const zip = $("#zipLink");
  zip.href = `/api/results/${sid}/zip`;
  zip.classList.remove("hidden");
  document.title = `(${doneCount}) Photo Upscale Studio`;
}

/* ---------------- скидання ---------------- */

function resetSession() {
  clearTimeout(state.pollTimer);
  location.reload();
}

/* ---------------- обробники подій ---------------- */

// настільний режим (pywebview): href-завантаження у WebView2 ненадійні,
// тому кнопки «Завантажити» перетворюються на нативний Save-діалог.
// Ім'я файлу береться з заголовка Content-Disposition сервера.
function hookDesktopDownloads() {
  document.addEventListener("click", (e) => {
    const api = window.pywebview && window.pywebview.api;
    if (!api || !api.save_url) return;
    const a = e.target.closest("a[download]");
    if (!a || !a.getAttribute("href")) return;
    e.preventDefault();
    const href = a.getAttribute("href");
    const aName = a.getAttribute("download");
    api.save_url(href, aName && aName !== "download" ? aName : "")
      .then(r => {
        if (r === "cancelled") return;
        if (String(r).startsWith("__")) pushError(String(r));
      })
      .catch(er => pushError("Не вдалося зберегти файл: " + (er && er.message
        ? er.message : er)));
  });
}

function initEvents() {
  const drop = $("#dropzone");
  const input = $("#fileInput");

  $("#pickBtn").addEventListener("click", () => input.click());
  input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });

  ["dragenter", "dragover"].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", e => addFiles(e.dataTransfer.files));

  // додатково: перетягування anywhere на сторінку не відкриває файл у браузері
  window.addEventListener("dragover", e => e.preventDefault());
  window.addEventListener("drop", e => e.preventDefault());

  // вставка з буфера обміну
  document.addEventListener("paste", (e) => {
    const files = Array.from(e.clipboardData?.files || []);
    if (files.length) addFiles(files);
  });

  $("#scaleSlide").addEventListener("input", updateScaleUI);
  $("#startBtn").addEventListener("click", startProcess);
  $("#resetBtn").addEventListener("click", resetSession);

  // збережений вибір пристрою
  try {
    const saved = localStorage.getItem("pusDevice");
    if (saved) $("#devSel").value = saved;
  } catch (_) {}

  hookDesktopDownloads();
}

initEvents();
loadInfo();
refreshEditor();

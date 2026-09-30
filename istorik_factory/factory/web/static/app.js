/* ИСТОРИК VIDEO FACTORY — панель. Без фреймворков: один лёгкий файл, обновления через SSE (/api/events), без опроса. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const nf = new Intl.NumberFormat("ru-RU");
const dtf = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const tf = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });

const STAGE_ICONS = {
  research: '<path d="M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z"/><path d="m21 21-4.3-4.3"/>',
  script: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4Z"/>',
  prompts: '<path d="M4 7V4h16v3"/><path d="M9 20h6"/><path d="M12 4v16"/>',
  images: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
  voice: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>',
  materials: '<path d="M21 8 12 3 3 8l9 5 9-5Z"/><path d="m3 13 9 5 9-5"/>',
  edit: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/>',
  verify: '<path d="M20 6 9 17l-5-5"/>',
};
const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
const icon = (k) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${STAGE_ICONS[k] || ""}</svg>`;
const STATUS = {
  created: ["создан", ""], running: ["в работе", "gold"], waiting_user: ["ждёт вас", "warn"], failed: ["ошибка", "err"],
  stopped: ["остановлен", ""], done: ["ГОТОВО", "ok"],
};

const S = {
  stages: [], state: null, topics: [], topicsUpdated: null, topicsStatus: {}, projects: [], keys: null, health: null,
  route: { name: "home" }, project: null, snapAt: 0, frames: [], framesFor: null, wave: null, waveFor: null,
  pendingStart: null, celebrated: new Set(JSON.parse(sessionStorage.getItem("celebrated") || "[]")),
};

/* ---------- сеть ---------- */
async function api(url, opt = {}) {
  const init = { ...opt, headers: { "Content-Type": "application/json", ...(opt.headers || {}) } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const r = await fetch(url, init);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.detail || `Ошибка ${r.status}`); e.fix = j.fix; throw e; }
  return j;
}
const post = (url, body) => api(url, { method: "POST", body: body || {} });
function fail(e) { toast(e.message || String(e), "error", { fix: e.fix }); }

/* ---------- тосты ---------- */
function toast(message, level = "info", opts = {}) {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.innerHTML = `<span class="bar" aria-hidden="true"></span><div><div>${esc(message)}</div>${opts.fix ? `<span class="fix">${esc(opts.fix)}</span>` : ""}</div>`;
  if (opts.action) {
    const b = document.createElement("button");
    b.className = "btn btn-sm"; b.type = "button"; b.textContent = opts.action.label;
    b.addEventListener("click", () => { opts.action.fn(); dismiss(); });
    el.appendChild(b);
  } else {
    const b = document.createElement("button");
    b.className = "btn btn-ghost btn-sm btn-icon"; b.type = "button"; b.setAttribute("aria-label", "Закрыть уведомление"); b.textContent = "×";
    b.addEventListener("click", () => dismiss());
    el.appendChild(b);
  }
  box.appendChild(el);
  while (box.children.length > 4) box.firstElementChild.remove();
  let gone = false;
  function dismiss() {
    if (gone) return; gone = true;
    el.classList.add("out");
    setTimeout(() => el.remove(), 230);
  }
  setTimeout(dismiss, opts.timeout || (level === "error" ? 9000 : 5000));
  return dismiss;
}

/* ---------- диалоги ---------- */
function openDlg(d) { if (!d.open) d.showModal(); }
function closeDlg(d) { if (d.open) d.close(); }
$$("dialog").forEach((d) => {
  d.addEventListener("click", (e) => { if (e.target === d) closeDlg(d); if (e.target.closest("[data-close]")) closeDlg(d); });
});
function confirmDlg(title, msg, label = "Подтвердить") {
  return new Promise((res) => {
    const d = $("#dlgConfirm");
    $("#dlgConfirmTitle").textContent = title; $("#dlgConfirmMsg").textContent = msg; $("#confirmGo").textContent = label;
    const go = () => { cleanup(); closeDlg(d); res(true); };
    const cancel = () => { cleanup(); res(false); };
    function cleanup() { $("#confirmGo").removeEventListener("click", go); d.removeEventListener("close", cancel); }
    $("#confirmGo").addEventListener("click", go);
    d.addEventListener("close", cancel, { once: true });
    openDlg(d);
  });
}

/* ---------- форматирование ---------- */
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
const plural = (n, a, b, c) => { n = Math.abs(n) % 100; const n1 = n % 10; return n > 10 && n < 20 ? c : n1 > 1 && n1 < 5 ? b : n1 === 1 ? a : c; };

/* ---------- маршруты ---------- */
function parseRoute() {
  const m = location.hash.match(/^#\/p\/(.+)$/);
  return m ? { name: "project", id: decodeURIComponent(m[1]) } : { name: "home" };
}
window.addEventListener("hashchange", () => { S.route = parseRoute(); render(); $("#main").focus({ preventScroll: true }); window.scrollTo(0, 0); });

function render() {
  const v = $("#view");
  v.classList.remove("view");
  requestAnimationFrame(() => v.classList.add("view"));  // плавная смена экрана
  if (S.route.name === "project") renderProjectView(S.route.id); else renderHome();
}

/* =================== ГЛАВНАЯ =================== */
function renderHome() {
  const running = S.state && S.state.busy && S.state.current;
  $("#view").innerHTML = `
    ${running ? `<a class="card card-pad enter row" href="#/p/${encodeURIComponent(running.id)}" style="text-decoration:none;color:inherit;margin-bottom:16px;border-color:rgba(232,182,76,.35)">
        <span class="spinner" aria-hidden="true"></span><div style="flex:1;min-width:0"><div class="muted" style="font-size:12.5px">Сейчас в работе</div>
        <div class="truncate" style="font-weight:600">${esc(running.title)}</div></div><span class="btn btn-sm">Открыть производство</span></a>` : ""}
    <div class="grid cols-main">
      <div class="stack">
        <section class="card hero enter" aria-labelledby="heroH">
          <h1 id="heroH">Выберите тему — дальше программа сделает видео сама</h1>
          <p class="lead">Исследование, сценарий, кадры, озвучка голосом Sadaltager, монтаж в ChatCut и проверка. Прогресс сохраняется после каждого шага — после сбоя работа продолжится с того же места.</p>
          <form class="compose" id="composeForm" autocomplete="off">
            <div class="field"><label for="customTitle">Своя тема</label>
              <input id="customTitle" name="custom_title" placeholder="Например: Вся история Казахского ханства…" spellcheck="true"></div>
            <div class="field"><label for="customMinutes">Длительность</label>
              <select id="customMinutes" name="minutes">${[1, 3, 5, 8, 10, 15, 20, 30].map((m) => `<option value="${m}" ${m === (S.state?.defaults?.target_minutes || 15) ? "selected" : ""}>${m} мин${m === 1 ? " — тест" : ""}</option>`).join("")}</select></div>
            <button class="btn btn-primary" type="submit" id="composeGo">Проверить и начать</button>
          </form>
        </section>
        <section aria-labelledby="topicsH">
          <div class="section-h"><h2 id="topicsH" class="grow">Актуальные темы</h2>
            <button class="btn btn-sm" type="button" id="topicsMore">Сгенерировать ещё</button>
            <button class="btn btn-sm btn-ghost" type="button" id="topicsRefresh">Обновить</button></div>
          <div id="topicsStatus" aria-live="polite"></div>
          <div class="topics" id="topicsGrid"></div>
        </section>
      </div>
      <aside class="stack" aria-label="Система и проекты">
        <section class="card card-pad enter" style="--i:1" aria-labelledby="healthH">
          <div class="section-h"><h2 id="healthH" class="grow">Готовность системы</h2><span id="healthBadge"></span></div>
          <ul class="health-list" id="healthList"></ul>
        </section>
        <section class="card card-pad enter" style="--i:2" aria-labelledby="projH">
          <div class="section-h"><h2 id="projH" class="grow">Проекты</h2></div>
          <div class="projects" id="projectsList"></div>
        </section>
      </aside>
    </div>`;
  $("#composeForm").addEventListener("submit", (e) => { e.preventDefault(); startCustom(); });
  $("#topicsMore").addEventListener("click", () => refreshTopics(true));
  $("#topicsRefresh").addEventListener("click", () => refreshTopics(false));
  renderTopics(); renderTopicsStatus(); renderHealth(); renderProjects();
}

function renderTopicsStatus() {
  const box = $("#topicsStatus"); if (!box) return;
  const st = S.topicsStatus || {};
  if (st.running) {
    box.innerHTML = `<div class="search-status"><span class="spinner" aria-hidden="true"></span><span>${esc(st.stage || "Ищу темы…")}</span>
      <span class="subtle num" style="margin-left:auto" data-since="${st.started || 0}"></span></div>`;
  } else if (st.error) {
    box.innerHTML = `<div class="alert"><b>${esc(st.error)}</b><span class="muted">${esc(st.fix || "")}</span>
      <div class="row"><button class="btn btn-sm" type="button" id="topicsRetry">Повторить поиск</button>
      <button class="btn btn-sm btn-ghost" type="button" id="topicsKeys">Ключи</button></div></div>`;
    $("#topicsRetry").addEventListener("click", () => refreshTopics(false));
    $("#topicsKeys").addEventListener("click", openKeys);
  } else if (S.topicsUpdated) {
    box.innerHTML = `<p class="hint" style="margin:-6px 0 10px">Обновлено ${esc(dtf.format(new Date(S.topicsUpdated)))} · учтены уже сделанные темы канала</p>`;
  } else box.innerHTML = "";
  tickSince();
  $("#topicsMore") && ($("#topicsMore").disabled = !!st.running || !S.topics.length);
  $("#topicsRefresh") && ($("#topicsRefresh").disabled = !!st.running);
}

function renderTopics() {
  const grid = $("#topicsGrid"); if (!grid) return;
  const st = S.topicsStatus || {};
  if (!S.topics.length) {
    if (st.running || (S.state && !S.state.missing_secrets.length && !st.error)) {
      grid.innerHTML = Array.from({ length: 4 }, (_, i) => `<div class="topic enter" style="--i:${i}" aria-hidden="true">
        <div class="skel skel-line" style="width:70%;height:18px"></div><div class="skel skel-line" style="width:40%"></div>
        <div class="skel skel-line"></div><div class="skel skel-line" style="width:85%"></div><div class="skel skel-line" style="width:60%"></div></div>`).join("");
    } else if (S.state && S.state.missing_secrets.length) {
      grid.innerHTML = `<div class="card empty" style="grid-column:1/-1"><b>Нужен ключ Google AI Studio</b>
        <span>С ключом программа сама найдёт актуальные темы через Google.</span>
        <button class="btn btn-primary" type="button" id="emptyKeys">Добавить ключи</button></div>`;
      $("#emptyKeys").addEventListener("click", openKeys);
    } else grid.innerHTML = `<div class="card empty" style="grid-column:1/-1">Тем пока нет. Нажмите «Обновить».</div>`;
    return;
  }
  grid.innerHTML = S.topics.map((t, i) => `
    <article class="topic enter" style="--i:${Math.min(i, 8)}" aria-labelledby="t-${esc(t.id)}">
      <div class="meta"><span class="badge gold">${esc(t.period || "период уточняется")}</span>
        ${t.suggested_minutes ? `<span class="badge num">${t.suggested_minutes} мин</span>` : ""}
        ${t.score != null ? `<span class="score num" style="margin-left:auto" aria-label="Оценка ${t.score} из 100">${t.score}</span>` : ""}</div>
      <h3 id="t-${esc(t.id)}">${esc(t.title)}</h3>
      <p class="why clamp-3">${esc(t.why_interesting)}</p>
      ${(t.key_events || []).length ? `<ul>${t.key_events.slice(0, 4).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
      <details><summary>Источники, конкуренты, почему подходит</summary><div class="dl">
        <div><b>Почему подходит каналу</b>${esc(t.fit || "—")}${t.angle ? `<br><i>Угол: ${esc(t.angle)}</i>` : ""}</div>
        <div><b>Источники</b>${(t.sources || []).slice(0, 5).map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.url)}</a>`).join("<br>") || "—"}</div>
        <div><b>Похожие видео конкурентов</b>${(t.competitor_videos || []).slice(0, 4).map((v) => `${v.url ? `<a href="${esc(v.url)}" target="_blank" rel="noopener noreferrer">${esc(v.title)}</a>` : esc(v.title)} <span class="subtle">${esc(v.channel || "")}${v.views ? ` · ${nf.format(v.views)} просм.` : ""}</span>`).join("<br>") || "—"}</div>
      </div></details>
      <div class="foot"><button class="btn btn-primary" type="button" data-topic="${esc(t.id)}">Выбрать тему</button></div>
    </article>`).join("");
  $$("[data-topic]", grid).forEach((b) => b.addEventListener("click", () => {
    const t = S.topics.find((x) => x.id === b.dataset.topic);
    openStart({ topic_id: t.id, title: t.title, minutes: t.suggested_minutes });
  }));
}

function renderHealth() {
  const list = $("#healthList"); if (!list) return;
  const h = S.health;
  if (!h || !h.items || !h.items.length) {
    list.innerHTML = Array.from({ length: 5 }, () => `<li aria-hidden="true"><span class="hdot"></span><div class="skel skel-line" style="width:70%"></div><span></span></li>`).join("");
    $("#healthBadge").innerHTML = `<span class="badge"><span class="spinner" style="width:10px;height:10px;border-width:2px" aria-hidden="true"></span>Проверяю…</span>`;
    return;
  }
  const errs = h.items.filter((i) => i.ok === false && i.severity === "error").length;
  const warns = h.items.filter((i) => i.ok === false && i.severity !== "error").length;
  $("#healthBadge").innerHTML = h.running ? `<span class="badge">Проверяю…</span>` :
    errs ? `<span class="badge err"><span class="dot"></span>${errs} ${plural(errs, "проблема", "проблемы", "проблем")}</span>` :
      warns ? `<span class="badge warn"><span class="dot"></span>готово, ${warns} ${plural(warns, "замечание", "замечания", "замечаний")}</span>` :
        `<span class="badge ok"><span class="dot"></span>всё готово</span>`;
  list.innerHTML = h.items.map((i) => `<li><span class="hdot ${i.ok ? "ok" : i.severity === "error" ? "err" : "warn"}" aria-hidden="true"></span>
    <div style="min-width:0"><div class="t">${esc(i.label)}</div><div class="d clamp-2">${esc(i.detail)}</div></div>
    ${!i.ok && i.fix ? `<button class="btn btn-sm" type="button" data-fix="${esc(i.fix)}" ${h.fixing && h.fixing[i.fix] ? "disabled" : ""}>${h.fixing && h.fixing[i.fix] ? "Исправляю…" : esc(i.fix_label)}</button>` : "<span></span>"}</li>`).join("");
  $$("[data-fix]", list).forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.fix === "keys") return openKeys();
    b.disabled = true; b.textContent = "Исправляю…";
    try { await post(`/api/health/fix/${b.dataset.fix}`); } catch (e) { fail(e); b.disabled = false; }
  }));
}

function renderProjects() {
  const box = $("#projectsList"); if (!box) return;
  if (!S.projects.length) { box.innerHTML = `<div class="empty">Пока нет проектов — выберите тему слева.</div>`; return; }
  const busy = S.state && S.state.busy;
  box.innerHTML = S.projects.slice(0, 30).map((p, i) => {
    const [label, cls] = STATUS[p.status] || [p.status || "—", ""];
    const cover = p.cover ? `<img class="cover" src="/api/projects/${encodeURIComponent(p.id)}/thumb/${p.cover.match(/(\d+)\.png$/)?.[1] || "001"}" width="92" height="52" alt="" loading="lazy">`
      : `<div class="cover empty" aria-hidden="true">И</div>`;
    const steps = S.stages.map(([k]) => `<i class="${esc(p.stages?.[k] || "")}"></i>`).join("");
    return `<article class="proj enter" style="--i:${Math.min(i, 8)}">${cover}<div style="min-width:0">
      <div class="row" style="gap:6px;flex-wrap:nowrap"><a class="t truncate" href="#/p/${encodeURIComponent(p.id)}" style="color:inherit;text-decoration:none;flex:1">${esc(p.title)}</a>
        <span class="badge ${cls}">${esc(label)}</span></div>
      <div class="mini-steps" aria-label="Готово этапов: ${p.done_stages} из 8">${steps}</div>
      ${p.error ? `<div class="d clamp-2" style="color:var(--err);font-size:12px;margin-top:4px">${esc(p.error)}</div>` : ""}
      <div class="acts"><a class="btn btn-sm" href="#/p/${encodeURIComponent(p.id)}">Открыть</a>
        ${p.status !== "done" && !busy ? `<button class="btn btn-sm" type="button" data-resume="${esc(p.id)}">Продолжить</button>` : ""}
        <button class="btn btn-sm btn-ghost" type="button" data-del="${esc(p.id)}" aria-label="Удалить проект ${esc(p.title)}">Удалить</button></div>
    </div></article>`;
  }).join("");
  $$("[data-resume]", box).forEach((b) => b.addEventListener("click", () => resume(b.dataset.resume)));
  $$("[data-del]", box).forEach((b) => b.addEventListener("click", () => delProject(b.dataset.del)));
}

/* ---------- действия: темы и старт ---------- */
async function refreshTopics(more) {
  try { await post(`/api/topics/refresh${more ? "?more=true" : ""}`); } catch (e) { fail(e); }
}

async function startCustom() {
  const raw = $("#customTitle").value.trim();
  if (!raw) { $("#customTitle").focus(); toast("Введите тему, например «Вся история Казахского ханства»", "warning"); return; }
  const btn = $("#composeGo"); btn.disabled = true; btn.textContent = "Проверяю название…";
  try {
    const r = await post("/api/topics/normalize", { title: raw });
    openStart({ title: r.title, raw, alternatives: r.alternatives || [], changed: r.changed, minutes: +$("#customMinutes").value });
  } catch (e) { fail(e); }
  finally { btn.disabled = false; btn.textContent = "Проверить и начать"; }
}

function openStart(o) {
  S.pendingStart = o;
  $("#startTitle").value = o.title;
  const chips = [...(o.alternatives || [])];
  if (o.changed && o.raw && o.raw !== o.title) chips.push(o.raw);
  $("#startSuggest").innerHTML = chips.length ? `<span class="hint">Варианты:</span>` + chips.map((c) => `<button class="chip" type="button">${esc(c)}</button>`).join("")
    + (o.changed ? `<span class="hint">Название исправлено автоматически</span>` : "") : "";
  $$("#startSuggest .chip").forEach((c) => c.addEventListener("click", () => { $("#startTitle").value = c.textContent; }));
  const sel = $("#startMinutes");
  const want = o.minutes || S.state?.defaults?.target_minutes || 15;
  sel.value = [...sel.options].map((x) => +x.value).reduce((a, b) => Math.abs(b - want) < Math.abs(a - want) ? b : a, 15);
  $("#startBackend").value = S.state?.defaults?.image_backend || "gemini_api";
  updateEstimate();
  openDlg($("#dlgStart"));
}
function updateEstimate() {
  const m = +$("#startMinutes").value;
  const frames = Math.round(m * (S.state?.defaults?.wpm || 150) / 15);
  $("#startEstimate").textContent = `≈ ${nf.format(m * (S.state?.defaults?.wpm || 150))} слов, ${frames} ${plural(frames, "кадр", "кадра", "кадров")}. Ориентировочно ${fmtDur(estimateTotal(m, frames, $("#startBackend").value))} работы.`;
}
$("#startMinutes").addEventListener("change", updateEstimate);
$("#startBackend").addEventListener("change", updateEstimate);
$("#startGo").addEventListener("click", async () => {
  const o = S.pendingStart; if (!o) return;
  const title = $("#startTitle").value.trim();
  const btn = $("#startGo"); btn.disabled = true; btn.textContent = "Запускаю…";
  try {
    const body = { target_minutes: +$("#startMinutes").value, image_backend: $("#startBackend").value };
    if (o.topic_id && title === o.title) body.topic_id = o.topic_id; else { body.custom_title = title; body.raw_title = o.raw || title; }
    const r = await post("/api/start", body);
    closeDlg($("#dlgStart"));
    location.hash = `#/p/${encodeURIComponent(r.id)}`;
    toast("Производство началось. Дальше всё автоматически.", "success");
  } catch (e) { fail(e); } finally { btn.disabled = false; btn.textContent = "Начать производство"; }
});

async function resume(id, from) {
  try { await post(`/api/projects/${encodeURIComponent(id)}/resume${from ? `?from_stage=${from}` : ""}`); location.hash = `#/p/${encodeURIComponent(id)}`; toast("Продолжаю с последнего завершённого шага", "success"); }
  catch (e) { fail(e); }
}
async function delProject(id) {
  const p = S.projects.find((x) => x.id === id);
  try {
    await post(`/api/projects/${encodeURIComponent(id)}/delete`);
    S.projects = S.projects.filter((x) => x.id !== id); renderProjects();
    if (S.route.name === "project" && S.route.id === id) location.hash = "#/";
    toast(`Проект «${p ? p.title : id}» перенесён в корзину`, "info", { timeout: 8000, action: { label: "Отменить", fn: async () => {
      try { await post(`/api/projects/${encodeURIComponent(id)}/restore`); await loadProjects(); toast("Проект восстановлен", "success"); } catch (e) { fail(e); }
    } } });
  } catch (e) { fail(e); }
}

/* =================== ПРОИЗВОДСТВО =================== */
async function renderProjectView(id) {
  $("#view").innerHTML = `<div class="stack">
    <div><a class="btn btn-ghost btn-sm" href="#/">← Все темы и проекты</a></div>
    <section class="card card-pad enter" aria-labelledby="prodTitle">
      <div class="prod-head"><div class="grow">
        <div class="row" style="gap:8px;margin-bottom:6px"><span id="prodStatus"></span><span class="subtle num" id="prodMeta"></span></div>
        <h1 id="prodTitle"><span class="skel skel-line" style="display:block;width:60%;height:30px"></span></h1></div>
        <div class="row" id="prodActions"></div></div>
      <ol class="pipeline" id="pipeline" aria-label="Этапы производства">${S.stages.map(([k, label], i) => `
        <li class="step" id="st-${k}" style="--i:${i}"><span class="line" aria-hidden="true"><i></i></span>
          <span class="node" aria-hidden="true">${icon(k)}</span><span class="name">${esc(label)}</span><span class="time" id="tm-${k}"></span></li>`).join("")}</ol>
      <div class="opbar" id="opbar"><span class="spinner" id="opSpin" aria-hidden="true"></span><div class="op" id="opText" aria-live="polite"><span></span></div>
        <div class="stats"><span class="big-pct" id="opPct">0%</span><span class="muted" id="opEta"></span></div></div>
      <div class="progress" style="margin-top:10px" role="progressbar" aria-label="Общий прогресс" aria-valuemin="0" aria-valuemax="100" id="totalBar"><i></i></div>
    </section>
    <div id="errorBox"></div>
    <div id="doneBox"></div>
    <section class="card card-pad" id="framesCard" hidden aria-labelledby="framesH">
      <div class="section-h"><h2 id="framesH" class="grow">Кадры</h2><span class="badge num" id="framesCount"></span></div>
      <div class="frames" id="framesGrid"></div></section>
    <section class="card card-pad" id="voiceCard" hidden aria-labelledby="voiceH">
      <div class="section-h"><h2 id="voiceH" class="grow">Озвучка</h2><span class="badge num" id="voiceDur"></span></div>
      <div class="wave"><button class="btn btn-icon" type="button" id="playBtn" aria-label="Прослушать озвучку">▶</button>
        <canvas id="waveCanvas" height="64" aria-label="Волна озвучки" role="img"></canvas><span class="subtle num" id="waveTime">0:00</span></div>
      <audio id="voiceAudio" preload="none"></audio></section>
    <section class="card card-pad"><details class="box" id="logBox"><summary id="logSummary">Журнал событий</summary><div class="log" id="log" tabindex="0"></div></details></section>
  </div>`;
  S.framesFor = null; S.waveFor = null; S.frames = []; S.framesFull = false;
  setupWave();
  $("#logSummary").addEventListener("click", () => { $("#logBox").dataset.touched = "1"; });
  try {
    const snap = (S.state && S.state.current && S.state.current.id === id) ? S.state.current : await api(`/api/projects/${encodeURIComponent(id)}`);
    applySnapshot(snap);
  } catch (e) { $("#prodTitle").textContent = "Проект не найден"; fail(e); }
}

function stageState(snap, k) {
  const st = snap.stages[k] || {};
  if (snap.status === "waiting_user" && snap.current_stage === k) return "waiting";
  return st.status || "pending";
}

const EXPECT = (k, m, frames, backend, chatcut) => ({
  research: 60, script: 25 + 7 * m, prompts: 10 + 1.2 * m, images: backend === "flow" ? 25 * frames : 20 + frames * 3,
  voice: 15 + 4 * m, materials: 12, edit: chatcut ? 150 + 12 * m : 3, verify: 15 + 3 * m,
}[k]);
function estimateTotal(m, frames, backend) { return S.stages.reduce((a, [k]) => a + EXPECT(k, m, frames, backend, true), 0); }

function progressModel(snap) {
  const m = +snap.target_minutes || 5;
  const frames = snap.stages.images?.progress?.total || Math.round(m * 10);
  const chatcut = (snap.chatcut || {}).mode !== "disabled";
  let total = 0, done = 0, remain = 0;
  const now = Date.now() / 1000, drift = (Date.now() - S.snapAt) / 1000;
  for (const [k] of S.stages) {
    const st = snap.stages[k] || {};
    const exp = EXPECT(k, m, frames, snap.image_backend, chatcut);
    total += exp;
    if (st.status === "done") { done += exp; continue; }
    if (st.status === "running") {
      const el = (st.elapsed_live || st.elapsed || 0) + (snap.status === "running" ? drift : 0);
      const pr = st.progress && st.progress.total ? st.progress.done / st.progress.total : 0;
      const frac = Math.min(0.97, pr || Math.min(0.6, el / exp));
      done += exp * frac;
      remain += pr > 0.08 ? el * (1 - pr) / pr : Math.max(exp - el, exp * 0.3);
    } else remain += exp;
  }
  return { pct: Math.round(100 * done / total), eta: remain };
}

function stageTime(snap, k) {
  const st = snap.stages[k] || {};
  let t = st.elapsed || 0;
  if (st.status === "running") t = (st.elapsed_live || st.elapsed || 0) + (snap.status === "running" ? (Date.now() - S.snapAt) / 1000 : 0);
  return t;
}

function applySnapshot(snap) {
  if (!snap || S.route.name !== "project" || S.route.id !== snap.id) return;
  S.project = snap; S.snapAt = Date.now();
  document.title = `${snap.status === "done" ? "✓ " : snap.status === "running" ? "▶ " : ""}${snap.title} — ИСТОРИК`;
  const title = $("#prodTitle"); if (title.textContent !== snap.title) title.textContent = snap.title;
  const [label, cls] = STATUS[snap.status] || [snap.status, ""];
  $("#prodStatus").innerHTML = `<span class="badge ${cls}"><span class="dot"></span>${esc(label)}</span>`;
  const m = +snap.target_minutes || 0;
  $("#prodMeta").textContent = `${m} мин · ${snap.image_backend === "flow" ? "Google Flow" : "Gemini API"}`;
  // действия
  const busyHere = S.state && S.state.busy && S.state.current && S.state.current.id === snap.id;
  const exportFile = (snap.result || {}).export_chatcut || (snap.result || {}).export_local;
  $("#prodActions").innerHTML = `
    ${busyHere && ["running", "waiting_user"].includes(snap.status) ? `<button class="btn btn-danger" type="button" id="bStop">Остановить</button>` : ""}
    ${!busyHere && snap.status !== "done" ? `<button class="btn btn-primary" type="button" id="bResume">Продолжить проект</button>` : ""}
    ${(snap.chatcut || {}).editor_url ? `<a class="btn" href="${esc(snap.chatcut.editor_url)}" target="_blank" rel="noopener noreferrer"><span translate="no">ChatCut</span></a>` : ""}
    <button class="btn" type="button" id="bFolder">Папка</button>`;
  $("#bStop") && $("#bStop").addEventListener("click", async () => {
    if (await confirmDlg("Остановить производство?", "Всё готовое сохранено. Нажмите «Продолжить проект», чтобы продолжить с того же места.", "Остановить")) {
      try { await post("/api/stop"); toast("Останавливаю…", "info"); } catch (e) { fail(e); }
    }
  });
  $("#bResume") && $("#bResume").addEventListener("click", () => resume(snap.id));
  $("#bFolder").addEventListener("click", () => post(`/api/open/${encodeURIComponent(snap.id)}`, { path: "" }).catch(fail));
  // этапы
  S.stages.forEach(([k], i) => {
    const el = $(`#st-${k}`); const s = stageState(snap, k); const st = snap.stages[k] || {};
    el.className = `step ${s}`;
    el.querySelector(".node").innerHTML = s === "done" ? CHECK : icon(k);
    const pr = st.progress && st.progress.total ? st.progress.done / st.progress.total : 0;
    el.querySelector(".line > i").style.transform = `scaleX(${s === "done" ? 1 : s === "running" ? Math.max(0.04, pr) : 0})`;
    if (window.innerWidth <= 760) el.querySelector(".line > i").style.transform = `scaleY(${s === "done" ? 1 : s === "running" ? Math.max(0.04, pr) : 0})`;
    el.setAttribute("aria-label", `${S.stages[i][1]}: ${s === "done" ? "готово" : s === "running" ? "идёт" : s === "failed" ? "ошибка" : "ожидает"}`);
  });
  tickTimers();
  // бегущая строка операции
  const op = snap.current_operation || (snap.status === "running" ? "Работаю…" : "");
  const opEl = $("#opText");
  const span = opEl.querySelector("span");
  if (span.dataset.t !== op) { span.dataset.t = op; span.textContent = op; }
  opEl.classList.toggle("run", false);
  $("#opSpin").style.visibility = snap.status === "running" ? "visible" : "hidden";
  const logBox = $("#logBox");
  if (logBox && !logBox.dataset.touched) logBox.open = ["running", "waiting_user", "failed"].includes(snap.status) && (snap.stages.prompts || {}).status !== "done";
  requestAnimationFrame(() => {
    const over = span.scrollWidth > opEl.clientWidth + 4 && snap.status === "running" && !reduced;
    if (over && !opEl.classList.contains("run")) { span.textContent = `${op}     ${op}`; opEl.classList.add("run"); }
  });
  // ошибка
  const err = snap.last_error;
  $("#errorBox").innerHTML = snap.status === "failed" && err ? `<div class="alert enter"><b>${esc(err.title)}</b><span>${esc(err.fix)}</span>
    <details><summary class="subtle" style="cursor:pointer">Подробности</summary><div class="log" style="margin-top:8px">${esc(err.detail)}</div></details>
    <div class="row"><button class="btn btn-primary btn-sm" type="button" id="errResume">Продолжить проект</button>
    <button class="btn btn-sm" type="button" id="errKeys">Ключи</button></div></div>` : "";
  $("#errResume") && $("#errResume").addEventListener("click", () => resume(snap.id));
  $("#errKeys") && $("#errKeys").addEventListener("click", openKeys);
  // действие пользователя
  const ua = snap.user_action; const dlg = $("#dlgAction");
  if (ua && snap.status === "waiting_user") {
    $("#dlgActionTitle").textContent = ua.title || "Требуется действие";
    $("#dlgActionMsg").textContent = ua.message;
    $("#dlgActionUrl").innerHTML = ua.url ? `Если окно не открылось: <a href="${esc(ua.url)}" target="_blank" rel="noopener noreferrer">открыть страницу входа</a>` : "";
    openDlg(dlg);
  } else closeDlg(dlg);
  // готово
  renderDone(snap, exportFile);
  // кадры и голос
  const pr = snap.stages.prompts || {};
  if (pr.status === "done" || (pr.status === "running" && pr.progress && pr.progress.total > 0)) {
    $("#framesCard").hidden = false;
    if (S.framesFor !== snap.id || (pr.status === "done" && !S.framesFull)) { S.framesFull = pr.status === "done"; loadFrames(snap.id); } else updateFramesCount();
  }
  const vs = snap.stages.voice || {};
  if (vs.status === "done" || (vs.progress && vs.progress.done > 0)) { $("#voiceCard").hidden = false; loadWave(snap.id, vs.status === "done"); }
  // журнал
  const log = $("#log"); const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent = (snap.log_tail || []).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function tickTimers() {
  const snap = S.project; if (!snap || !$("#pipeline")) return;
  for (const [k] of S.stages) {
    const t = stageTime(snap, k); const st = snap.stages[k] || {};
    const el = $(`#tm-${k}`);
    if (el) el.textContent = st.status === "done" || st.status === "running" || t > 0 ? fmtDur(t) : "";
  }
  const pm = progressModel(snap);
  const pct = snap.status === "done" ? 100 : pm.pct;
  $("#opPct").textContent = `${pct}%`;
  $("#totalBar").setAttribute("aria-valuenow", pct);
  $("#totalBar > i").style.transform = `scaleX(${pct / 100})`;
  const total = S.stages.reduce((a, [k]) => a + stageTime(snap, k), 0);
  $("#opEta").textContent = snap.status === "done" ? `за ${fmtDur(total)}` : snap.status === "running" ? `прошло ${fmtDur(total)} · осталось ≈ ${fmtDur(pm.eta)}` :
    snap.status === "waiting_user" ? "ждёт вашего действия" : "";
}
function tickSince() { $$("[data-since]").forEach((el) => { const t = +el.dataset.since; if (t) el.textContent = fmtDur(Date.now() / 1000 - t); }); }
setInterval(() => { if (S.route.name === "project" && S.project && ["running", "waiting_user"].includes(S.project.status)) tickTimers(); tickSince(); }, 1000);

function renderDone(snap, exportFile) {
  const box = $("#doneBox");
  if (snap.status !== "done") { box.innerHTML = ""; return; }
  if (box.dataset.for === snap.id) return;
  box.dataset.for = snap.id;
  const r = snap.result || {};
  const rel = (f) => f ? f.replace(/\\/g, "/").split(`/${snap.id}/`).pop() : null;
  const local = rel(r.export_local), cc = rel(r.export_chatcut);
  const video = cc || local;
  box.innerHTML = `<section class="done-banner enter" aria-labelledby="readyH">
    <div class="ready" id="readyH">ГОТОВО</div>
    <p class="muted" style="margin:8px 0 16px">${esc(snap.title)} · ${esc(r.duration_text || "")} · ${nf.format(r.words || 0)} слов · кадров ${r.frames_done || 0}/${r.frames_total || 0}</p>
    ${video ? `<video controls preload="metadata" playsinline src="/api/projects/${encodeURIComponent(snap.id)}/file?path=${encodeURIComponent(video)}" poster="/api/projects/${encodeURIComponent(snap.id)}/thumb/001"></video>` : ""}
    <div class="row" style="justify-content:center;margin-top:14px">
      ${video ? `<button class="btn btn-primary" type="button" data-open="${esc(video)}">Открыть MP4</button>` : ""}
      ${(snap.chatcut || {}).editor_url ? `<a class="btn" href="${esc(snap.chatcut.editor_url)}" target="_blank" rel="noopener noreferrer">Открыть в <span translate="no">ChatCut</span></a>` : ""}
      <button class="btn" type="button" data-open="">Папка проекта</button></div>
    <details style="margin-top:16px;text-align:left"><summary class="muted" style="cursor:pointer">Отчёт</summary><div class="report">${esc(r.report || "")}</div></details>
  </section>`;
  $$("[data-open]", box).forEach((b) => b.addEventListener("click", () => post(`/api/open/${encodeURIComponent(snap.id)}`, { path: b.dataset.open }).catch(fail)));
  if (!S.celebrated.has(snap.id) && (Date.now() / 1000 - Date.parse(snap.stages.verify?.finished_at || 0) / 1000) < 600) {
    S.celebrated.add(snap.id); sessionStorage.setItem("celebrated", JSON.stringify([...S.celebrated]));
    box.firstElementChild.classList.add("flash"); confetti();
    box.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
  }
}

/* ---------- кадры ---------- */
async function loadFrames(id) {
  S.framesFor = id;
  try { S.frames = await api(`/api/projects/${encodeURIComponent(id)}/frames`); } catch { S.frames = []; }
  const grid = $("#framesGrid"); if (!grid) return;
  grid.innerHTML = S.frames.map((f) => `<figure class="frame ${f.status === "failed" ? "failed" : ""}" id="fr-${f.frame_id}" style="margin:0" title="${esc(f.text)}">
    ${f.status === "done" ? frameImg(id, f.frame_id) : `<div class="skel" style="position:absolute;inset:0" aria-hidden="true"></div>`}
    <span class="n">${f.frame_id}</span></figure>`).join("");
  hookImgs(grid); updateFramesCount();
}
const frameImg = (id, fid) => `<img src="/api/projects/${encodeURIComponent(id)}/thumb/${fid}" width="192" height="108" alt="Кадр ${fid}" loading="lazy" decoding="async">`;
function hookImgs(root) { $$("img", root).forEach((im) => { if (im.complete) im.classList.add("loaded"); else im.addEventListener("load", () => im.classList.add("loaded"), { once: true }); }); }
function onFrame(d) {
  if (!S.project || d.project !== S.project.id) return;
  const f = S.frames.find((x) => x.frame_id === d.frame_id); if (f) f.status = "done";
  const el = $(`#fr-${d.frame_id}`);
  if (el && !el.querySelector("img")) { el.classList.remove("failed"); el.querySelector(".skel")?.remove(); el.insertAdjacentHTML("afterbegin", frameImg(d.project, d.frame_id)); hookImgs(el); }
  else if (!el && S.framesFor === d.project) loadFrames(d.project);
  updateFramesCount();
}
function updateFramesCount() { const n = S.frames.filter((f) => f.status === "done").length; const c = $("#framesCount"); if (c) c.textContent = `${n} / ${S.frames.length}`; }

/* ---------- волна озвучки ---------- */
let waveTimer = null;
function loadWave(id, final) {
  if (S.waveFor === `${id}:${final}`) return;
  clearTimeout(waveTimer);
  waveTimer = setTimeout(async () => {
    try { S.wave = await api(`/api/projects/${encodeURIComponent(id)}/waveform`); S.waveFor = `${id}:${final}`; } catch { return; }
    $("#voiceDur").textContent = S.wave.duration ? fmtDur(S.wave.duration) : "";
    const a = $("#voiceAudio");
    const src = final ? `/api/projects/${encodeURIComponent(id)}/file?path=05_voice/master_voice.wav` : `/api/projects/${encodeURIComponent(id)}/file?path=05_voice/voice_001.wav`;
    if (!a.src.endsWith(src)) a.src = src;
    drawWave();
  }, 400);
}
function setupWave() {
  const a = $("#voiceAudio"), btn = $("#playBtn"), cv = $("#waveCanvas");
  btn.addEventListener("click", () => { if (a.paused) a.play().catch((e) => fail(e)); else a.pause(); });
  a.addEventListener("play", () => { btn.textContent = "❚❚"; btn.setAttribute("aria-label", "Пауза"); loop(); });
  a.addEventListener("pause", () => { btn.textContent = "▶"; btn.setAttribute("aria-label", "Прослушать озвучку"); });
  a.addEventListener("ended", () => { btn.textContent = "▶"; drawWave(); });
  cv.addEventListener("click", (e) => { if (!a.duration) return; const r = cv.getBoundingClientRect(); a.currentTime = a.duration * (e.clientX - r.left) / r.width; drawWave(); });
  function loop() { drawWave(); $("#waveTime").textContent = fmtDur(a.currentTime); if (!a.paused) requestAnimationFrame(loop); }
  new ResizeObserver(() => drawWave()).observe(cv);
}
function drawWave() {
  const cv = $("#waveCanvas"); if (!cv || !S.wave) return;
  const dpr = devicePixelRatio || 1, w = cv.clientWidth, h = 64;
  if (!w) return;
  cv.width = w * dpr; cv.height = h * dpr;
  const ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
  const peaks = S.wave.peaks || []; const n = Math.max(1, Math.floor(w / 3));
  const a = $("#voiceAudio"); const played = a && a.duration ? a.currentTime / a.duration : 0;
  for (let i = 0; i < n; i++) {
    const v = peaks[Math.floor(i * peaks.length / n)] || 0; const bh = Math.max(2, v * (h - 6));
    ctx.fillStyle = i / n <= played ? "#f5cf73" : "rgba(232,182,76,.38)";
    ctx.fillRect(i * 3, (h - bh) / 2, 2, bh);
  }
}

/* ---------- конфетти (золото), уважает reduced-motion ---------- */
function confetti() {
  if (reduced) return;
  const cv = $("#confetti"), ctx = cv.getContext("2d"); const dpr = devicePixelRatio || 1;
  cv.width = innerWidth * dpr; cv.height = innerHeight * dpr; ctx.scale(dpr, dpr);
  const colors = ["#f5cf73", "#e8b64c", "#fbdc8f", "#b7791f", "#fafafa"];
  const parts = Array.from({ length: 140 }, () => ({ x: innerWidth / 2 + (Math.random() - 0.5) * 200, y: innerHeight * 0.35, vx: (Math.random() - 0.5) * 14,
    vy: -Math.random() * 13 - 4, r: Math.random() * Math.PI, vr: (Math.random() - 0.5) * 0.3, w: 5 + Math.random() * 6, h: 3 + Math.random() * 4, c: colors[Math.random() * colors.length | 0] }));
  const t0 = performance.now();
  (function frame(t) {
    const k = (t - t0) / 2400; ctx.clearRect(0, 0, innerWidth, innerHeight);
    for (const p of parts) { p.vy += 0.32; p.vx *= 0.99; p.x += p.vx; p.y += p.vy; p.r += p.vr;
      ctx.save(); ctx.globalAlpha = Math.max(0, 1 - k); ctx.translate(p.x, p.y); ctx.rotate(p.r); ctx.fillStyle = p.c; ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); ctx.restore(); }
    if (k < 1) requestAnimationFrame(frame); else ctx.clearRect(0, 0, innerWidth, innerHeight);
  })(t0);
}

/* =================== КЛЮЧИ =================== */
function renderKeysPill() {
  const k = S.keys; const pill = $("#keysPill");
  if (!k) return;
  const cls = !k.total ? "err" : k.ok + k.unknown === 0 ? "err" : k.invalid || k.quota ? "warn" : "ok";
  pill.className = `badge pill-btn ${cls}`;
  pill.innerHTML = `<span class="dot" aria-hidden="true"></span>${k.total ? `${k.total} ${plural(k.total, "ключ", "ключа", "ключей")} · ${k.ok + k.unknown} ✓` : "Добавить ключи"}`;
  pill.setAttribute("aria-label", `Ключи Gemini: ${k.text}`);
  if ($("#dlgKeys").open) renderKeysList();
}
function renderKeysList() {
  const k = S.keys || { keys: [] };
  $("#keysSummary").innerHTML = `<span class="badge ${k.invalid ? "warn" : "ok"}">${esc(k.text || "")}</span>${k.next_reset ? `<span class="hint">ближайший сброс квоты ≈ ${tf.format(new Date(k.next_reset * 1000))}</span>` : ""}`;
  $("#keysList").innerHTML = (k.keys || []).map((x) => `<li><span class="subtle num">№${x.index}</span><code class="truncate" translate="no">${esc(x.mask)}</code>
    ${x.status === "ok" ? `<span class="badge ok">работает</span>` : x.status === "quota" ? `<span class="badge warn">квота до ${x.until ? tf.format(new Date(x.until * 1000)) : "…"}</span>` :
      x.status === "invalid" ? `<span class="badge err" title="${esc(x.reason)}">неверный</span>` : `<span class="badge">не проверен</span>`}</li>`).join("") || `<li><span></span><span class="muted">Ключей пока нет</span><span></span></li>`;
  $("#keysRemoveBad").disabled = !k.invalid;
}
function openKeys() { renderKeysList(); openDlg($("#dlgKeys")); setTimeout(() => $("#keysText").focus(), 60); }
$("#keysPill").addEventListener("click", openKeys);
$("#keysSave").addEventListener("click", async () => {
  const btn = $("#keysSave"); btn.disabled = true; btn.textContent = "Проверяю ключи…";
  try {
    S.keys = await post("/api/keys", { text: $("#keysText").value, replace: $("#keysReplace").checked, YOUTUBE_API_KEY: $("#ytKey").value });
    $("#keysText").value = ""; $("#ytKey").value = ""; $("#keysReplace").checked = false;
    renderKeysPill(); renderKeysList(); toast(`Ключи сохранены: ${S.keys.text}`, "success");
    const st = await api("/api/state"); S.state = st; if (S.route.name === "home") { renderTopics(); renderTopicsStatus(); }
  } catch (e) { fail(e); } finally { btn.disabled = false; btn.textContent = "Сохранить ключи"; }
});
$("#keysCheck").addEventListener("click", async () => {
  const btn = $("#keysCheck"); btn.disabled = true; btn.textContent = "Проверяю…";
  try { S.keys = await post("/api/keys/check"); renderKeysPill(); renderKeysList(); } catch (e) { fail(e); }
  finally { btn.disabled = false; btn.textContent = "Проверить ключи"; }
});
$("#keysRemoveBad").addEventListener("click", async () => {
  try { S.keys = await post("/api/keys/remove_invalid"); renderKeysPill(); renderKeysList(); toast("Неверные ключи удалены", "success"); } catch (e) { fail(e); }
});
$("#healthBtn").addEventListener("click", async () => {
  try { await post("/api/health/run"); toast("Проверяю систему…", "info", { timeout: 2500 }); if (S.route.name !== "home") location.hash = "#/"; } catch (e) { fail(e); }
});
$("#actionContinue").addEventListener("click", () => { post("/api/continue").catch(fail); closeDlg($("#dlgAction")); });
$("#actionStop").addEventListener("click", async () => { closeDlg($("#dlgAction")); try { await post("/api/stop"); } catch (e) { fail(e); } });

/* =================== ДАННЫЕ И СОБЫТИЯ =================== */
async function loadProjects() { try { S.projects = await api("/api/projects"); } catch { S.projects = []; } renderProjects(); }
async function loadTopics() {
  try { const t = await api("/api/topics"); S.topics = t.topics || []; S.topicsUpdated = t.updated_at; S.topicsStatus = t.status || {}; } catch { /* покажем скелет */ }
  renderTopics(); renderTopicsStatus();
}

function connect() {
  const es = new EventSource("/api/events");
  const on = (k, fn) => es.addEventListener(k, (e) => { try { fn(JSON.parse(e.data)); } catch (err) { console.warn(k, err); } });
  on("project", (d) => {
    if (S.state) S.state.current = S.state.busy && S.state.current && S.state.current.id !== d.id ? S.state.current : d;
    applySnapshot(d);
    const p = S.projects.find((x) => x.id === d.id);
    if (p && (p.status !== d.status || p.done_stages !== Object.values(d.stages).filter((s) => s.status === "done").length)) loadProjects();
  });
  on("runner", (d) => { if (S.state) S.state.busy = d.busy; loadProjects(); if (S.project && S.route.name === "project") applySnapshot(S.project); });
  on("topics_status", (d) => { S.topicsStatus = d; renderTopicsStatus(); if (!S.topics.length) renderTopics(); });
  on("topics", () => loadTopics());
  on("keys", (d) => { S.keys = d; renderKeysPill(); });
  on("health", (d) => { S.health = d; renderHealth(); });
  on("toast", (d) => toast(d.message, d.level, { fix: d.fix }));
  on("frame", onFrame);
  on("voice", (d) => { if (S.project && d.project === S.project.id) { S.waveFor = null; loadWave(d.project, false); } });
  es.addEventListener("open", () => { if (S.lostConn) { S.lostConn = false; boot(true); } });
  es.addEventListener("error", () => { S.lostConn = true; });
}

async function boot(again) {
  try {
    const st = await api("/api/state");
    S.state = st; S.stages = st.stages; S.keys = st.keys; S.health = st.health; S.topicsStatus = st.topics_status || {};
    $("#channelLine").textContent = `канал «${st.channel || "ИСТОРИК"}»${st.mock ? " · тестовый режим" : ""}`;
    renderKeysPill();
  } catch (e) { fail(e); return; }
  S.route = parseRoute();
  if (!again) render();
  await Promise.all([loadTopics(), loadProjects()]);
  if (again && S.route.name === "project") renderProjectView(S.route.id);
  if (!again && S.state.missing_secrets.length) setTimeout(openKeys, 400);
}

boot(false).then(connect);

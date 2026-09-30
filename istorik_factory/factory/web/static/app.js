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
  const ctl = new AbortController();  // ни один запрос не висит бесконечно
  const timer = setTimeout(() => ctl.abort(), opt.timeout || 20000);
  init.signal = ctl.signal;
  let r;
  try { r = await fetch(url, init); }
  catch (e) { const err = new Error(e.name === "AbortError" ? "Программа не ответила вовремя" : "Нет связи с программой"); err.offline = true; throw err; }
  finally { clearTimeout(timer); }
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
          <p class="lead">Исследование, сценарий, кадры, озвучка, монтаж в ChatCut и проверка. Кончился лимит у одного источника — работу продолжит следующий. Прогресс сохраняется после каждого шага — после сбоя работа продолжится с того же места.</p>
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
        <section class="card card-pad enter" style="--i:2" aria-labelledby="usageH" id="limitsCard">
          <div class="section-h"><h2 id="usageH" class="grow">Лимиты на сегодня</h2><span class="hint num" id="usageReset"></span></div>
          <div id="usageBox" aria-live="polite"></div>
        </section>
        <section class="card card-pad enter" style="--i:4" aria-labelledby="projH">
          <div class="section-h"><h2 id="projH" class="grow">Проекты</h2></div>
          <div class="projects" id="projectsList"></div>
        </section>
      </aside>
    </div>
    <section class="card card-pad enter sources" style="--i:5" aria-labelledby="provH" id="sourcesCard">
      <div class="section-h"><h2 id="provH" class="grow">Источники</h2>
        <button class="btn btn-sm btn-primary" type="button" id="recAll">Рекомендовать всё</button></div>
      <p class="hint" style="margin:-4px 0 12px"><span id="provHw"></span><br>Работает первый готовый источник; если у него кончился лимит или он сломался — дальше по списку, автоматически.
        <span>∞ без лимита · 🔑 ключ · ⭐ подписка · 💳 платно · ⚠ неофициально</span></p>
      <div id="provBox" class="prov-grid"></div>
    </section>`;
  $("#recAll").addEventListener("click", (e) => recommendProv("all", e.currentTarget));
  $("#composeForm").addEventListener("submit", (e) => { e.preventDefault(); startCustom(); });
  $("#topicsMore").addEventListener("click", () => refreshTopics(true));
  $("#topicsRefresh").addEventListener("click", () => refreshTopics(false));
  renderTopics(); renderTopicsStatus(); renderHealth(); renderProjects(); renderUsage(); renderProviders();
}

/* ---------- лимиты ---------- */
const clock = (t) => (t ? tf.format(new Date(t * 1000)) : "");
function meter(pct, label) {
  const cls = pct == null ? "" : pct <= 5 ? "err" : pct <= 25 ? "warn" : "ok";
  return { cls, bar: `<div class="progress usage-bar ${cls}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct ?? 0}" aria-label="Осталось лимита: ${esc(label)}"><i style="transform:scaleX(${(pct ?? 0) / 100})"></i></div>` };
}
const pctText = (p) => (p == null ? "—" : `${p.toFixed(1).replace(".0", "")}%`);
function renderUsage() {
  const box = $("#usageBox"); if (!box) return;
  const u = S.usage;
  if (!u) { box.innerHTML = `<div class="skel skel-line"></div>`; return; }
  const g = u.gemini || {};
  $("#usageReset").textContent = g.reset_at ? `Gemini: сброс в ${clock(g.reset_at)}` : "";
  const models = g.models || [];
  const gem = models.map((m) => {
    const { cls, bar } = meter(m.pct, m.label);
    const projects = m.projects || [];
    return `<details class="usage-row"><summary>
        <div class="usage-top"><span class="t">Gemini · ${esc(m.label)}</span><code class="subtle" translate="no">${esc(m.model)}</code>
          <b class="num ${cls}" style="margin-left:auto">${pctText(m.pct)}</b></div>${bar}
        <div class="hint num">осталось ${nf.format(m.left)} из ${nf.format(m.limit)} · ${m.exact ? "точно (лимит сообщил Google)" : "оценка"} · сброс в ${clock(g.reset_at)}</div>
      </summary>
      <ul class="usage-keys">${projects.map((p) => `<li><span class="subtle">${p.exhausted ? "⛔" : "●"}</span>
        <span class="truncate">${esc(p.label)} <span class="subtle num">(ключи ${p.keys.map((k) => `№${k}`).join(", ")})</span></span>
        <span class="num">${p.exhausted ? `<span class="badge err">исчерпан до ${clock(g.reset_at)}</span>` : `${nf.format(p.left)} / ${nf.format(p.limit)}`}</span></li>`).join("")}</ul>
    </details>`;
  }).join("");
  const rows = u.providers || [];
  const api = rows.filter((r) => !r.unlimited).map((r) => {
    const { cls, bar } = meter(r.pct, r.label);
    return `<div class="usage-row"><div class="usage-top"><span class="t">${esc(r.label)}</span><code class="subtle" translate="no">${esc(r.model)}</code>
        <b class="num ${cls}" style="margin-left:auto">${pctText(r.pct)}</b></div>${r.limit ? bar : ""}
      <div class="hint num">${r.limit ? `осталось ${nf.format(r.left ?? 0)} из ${nf.format(r.limit)} · ${r.exact ? "точно (из ответа сервиса)" : "оценка"}` : `использовано ${nf.format(r.used)} · лимит неизвестен`}${r.reset_at ? ` · сброс в ${clock(r.reset_at)}` : ""}</div></div>`;
  }).join("");
  const local = rows.filter((r) => r.unlimited).map((r) => `<div class="usage-row"><div class="usage-top"><span class="t">${esc(r.label)}</span>
      <code class="subtle" translate="no">${esc(r.model)}</code><b class="num ok" style="margin-left:auto">∞</b></div>
      <div class="hint num">без лимита · сегодня ${nf.format(r.used)} ${plural(r.used, "запрос", "запроса", "запросов")}</div></div>`).join("");
  if (!gem && !api && !local) {
    box.innerHTML = `<p class="hint" style="margin:0">Сегодня запросов ещё не было${g.keys ? ` · ключей Gemini: ${g.keys}${g.projects ? `, проектов: ${g.projects}` : ""}` : ""}.</p>`;
    return;
  }
  box.innerHTML = gem + api + local + `<p class="hint" style="margin:8px 0 0">Считаются запросы этой программы. Ключи одного проекта Google делят один лимит — укажите проект у ключей в окне «Ключи».</p>`;
}

/* ---------- источники ---------- */
const PARTS = [["text", "Текст", "исследование, сценарий, промты"], ["voice", "Озвучка", "голос диктора"], ["images", "Кадры", "иллюстрации к ролику"]];
const BADGE_CLS = { free: "ok", key: "", sub: "gold", paid: "warn", unofficial: "warn" };
const OPT_OF = { ollama: ["ollama_model", "ollama_models", "Модель"], piper: ["piper_voice", "piper_voices", "Голос"],
  silero: ["silero_speaker", "silero_speakers", "Голос"], edge: ["edge_voice", "edge_voices", "Голос"], comfyui: ["comfy_model", "comfy_models", "Модель"] };
function provInfo(part, id) { return (S.providers?.catalog?.[part] || []).find((x) => x.id === id) || { id, label: id, badge_text: "" }; }
function provReady(part, it) {
  const P = S.providers, st = P.status || {}, ins = (st.install || {})[it.install] || {};
  const route = (P.routes?.[part]?.chain || []).find((x) => x.id === it.id) || {};
  const out = { ok: true, text: "", act: "" };
  if (ins.running) return { ok: false, busy: true, text: ins.text || "Устанавливаю…", pct: ins.pct };
  if (route.cool_until && route.cool_until * 1000 > Date.now()) {
    return { ok: false, cool: true, text: `${route.reason || "лимит"} — пропускаю до ${clock(route.cool_until)}` };
  }
  if (it.needs_ack && P.settings.opts.screen_ack !== "1") {
    return { ok: false, text: "Экранный режим выключен — включите его выше, если согласны с предупреждением" };
  }
  if (it.id === "flow" && P.settings.opts.flow_subscription !== "1") out.text = "отметьте подписку AI Pro выше";
  if (it.secret && !it.optional_secret && !P.secrets[it.secret]) {
    return { ok: false, text: "Нужен ключ", act: `<button class="btn btn-sm" type="button" data-keys="${esc(it.secret)}">Ввести ключ</button>` };
  }
  if (it.id === "omniroute") {
    const s = st.omniroute || {};
    if (s.checking) return { ok: false, text: "проверяю OmniRoute…" };
    if (!s.installed) return { ok: false, text: ins.error ? `Ошибка: ${ins.error}` : "Не установлен (нужен Node.js — поставится сам)", err: !!ins.error, act: installBtn("omniroute", "Установить и запустить") };
    if (!s.running) return { ok: false, text: s.error ? `Не запущен: ${s.error}` : "Установлен, но не запущен", act: installBtn("omniroute", "Запустить") };
    out.text = `запущен · модель ${s.model || "auto"}`;
    if (route.active) out.active = true;
    return out;
  }
  if (it.install) {
    const s = st[it.install] || {};
    if (it.id === "ollama") {
      if (!s.installed) return { ok: false, text: ins.error ? `Ошибка: ${ins.error}` : "Ollama не установлена", err: !!ins.error, act: installBtn(it.install) };
      if (s.checking) return { ok: false, text: "проверяю Ollama…" };
      if (!s.has_model) return { ok: false, text: ins.error ? `Ошибка: ${ins.error}` : "В Ollama нет ни одной модели", err: !!ins.error, act: installBtn(it.install, "Скачать модель") };
      out.text = s.running ? `модель: ${s.model}${s.note ? ` (${s.note})` : ""}` : "запустится автоматически";
    } else if (!s.installed) {
      return { ok: false, text: ins.error ? `Ошибка: ${ins.error}` : "Не установлено", err: !!ins.error, act: installBtn(it.install) };
    }
    if (it.id === "chatterbox" && !s.sample) {
      return { ok: false, text: "Нужен образец голоса 10–30 с", act: `<label class="btn btn-sm file-btn">Загрузить образец<input type="file" accept="audio/*" data-sample hidden></label>` };
    }
  }
  if (route.active) out.active = true;
  return out;
}
function omniBlock(P) {
  const s = (P.status || {}).omniroute || {};
  const cur = P.settings.opts.omniroute_model || "auto";
  const models = (s.models && s.models.length ? s.models : ["auto"]);
  const t = S.omniTest;
  return `<div class="omni">
    ${s.running ? `<select class="prov-opt" data-opt="omniroute_model" aria-label="Модель OmniRoute">${models.map((m) => `<option value="${esc(m)}" ${m === cur ? "selected" : ""}>${esc(m)}${m === "auto" ? " — сам выбирает бесплатный провайдер" : ""}</option>`).join("")}</select>` : ""}
    <div class="row" style="gap:8px">
      <button class="btn btn-sm" type="button" data-test="omniroute" ${s.running ? "" : "disabled"}>${t && t.running ? "Проверяю…" : "Проверить"}</button>
      <a class="btn btn-sm btn-ghost" href="${esc(s.dashboard || "http://localhost:20128/dashboard")}" target="_blank" rel="noopener noreferrer">Панель OmniRoute ↗</a>
    </div>
    ${t && !t.running ? `<p class="hint ${t.ok ? "" : "err-text"}" style="margin:0">${t.ok ? `✓ ${esc(t.model || "")} за ${t.seconds} с: «${esc(t.text)}»` : `✗ ${esc(t.error)}${t.fix ? ` — ${esc(t.fix)}` : ""}`}</p>` : ""}
    <details class="omni-help"><summary>Как добавить свои аккаунты (ChatGPT, Claude, Grok, Gemini…)</summary>
      <ol class="hint">
        <li>Откройте «Панель OmniRoute» (кнопка выше) → раздел <b>Providers</b>.</li>
        <li>Выберите сервис: ChatGPT/Claude/Grok — вход через OAuth кнопкой; Gemini, Groq, OpenRouter, DeepSeek — вставьте API-ключ.</li>
        <li>Модель «auto» сразу начнёт использовать новые аккаунты и переключаться между ними при лимитах.</li>
        <li>Без аккаунтов работают встроенные бесплатные провайдеры OmniRoute (у каждого свой лимит).</li>
        ${s.auth_required ? `<li>Полный список моделей OmniRoute отдаёт только с ключом шлюза: создайте его в <b>Endpoints</b> и вставьте в «Ключи» → OmniRoute.</li>` : ""}
      </ol></details>
  </div>`;
}
async function testProvider(pid, btn) {
  S.omniTest = { running: true }; renderProviders();
  try { S.omniTest = await post(`/api/providers/test/${pid}`); }
  catch (e) { S.omniTest = { ok: false, error: e.message }; }
  renderProviders();
}
function installBtn(name, label = "Установить") { return `<button class="btn btn-sm" type="button" data-install="${esc(name)}">${label}</button>`; }

function renderProviders() {
  const box = $("#provBox"); if (!box) return;
  const P = S.providers; if (!P) { box.innerHTML = `<div class="skel skel-line"></div><div class="skel skel-line"></div>`; return; }
  $("#provHw").textContent = P.hw_text || "";
  const O = P.settings.opts;
  const modeSel = (part) => `<select class="prov-mode" data-mode="${part}" aria-label="Режим: ${part}">${[["hybrid", "Гибрид"], ["background", "Фон"], ["screen", "Экран"]]
    .map(([v, l]) => `<option value="${v}" ${O["mode_" + part] === v ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  const head = `<div class="prov-flags">
    <label class="row" style="gap:8px"><input type="checkbox" id="flowSub" ${O.flow_subscription === "1" ? "checked" : ""}> У меня есть Google AI Pro и доступ к Flow</label>
    <label class="row" style="gap:8px"><input type="checkbox" id="screenAck" ${O.screen_ack === "1" ? "checked" : ""}> Экранный режим: Gemini и AI Studio в моём Chrome</label>
    <span class="hint">Экранный режим медленнее API и, вероятно, нарушает правила Google об автоматическом доступе — аккаунт могут ограничить. На любой проверке (вход, CAPTCHA) программа останавливается и ждёт вас.</span></div>`;
  box.innerHTML = head + `<div class="prov-grid-inner">` + PARTS.map(([part, title, sub]) => {
    const chain = P.settings.chains[part] || [];
    const rest = (P.catalog[part] || []).filter((x) => !chain.includes(x.id));
    const items = chain.map((id, i) => {
      const it = provInfo(part, id), r = provReady(part, it);
      const o = OPT_OF[id];
      const optSel = o ? `<select class="prov-opt" data-opt="${o[0]}" aria-label="${o[2]} — ${esc(it.label)}">${Object.entries(P.options[o[1]] || {}).map(([k, v]) =>
        `<option value="${esc(k)}" ${P.settings.opts[o[0]] === k ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>` : "";
      const state = r.busy ? `<div class="prov-state"><span class="spinner" aria-hidden="true"></span><span class="hint">${esc(r.text)}</span></div>
          ${r.pct != null ? `<div class="progress"><i style="transform:scaleX(${r.pct / 100})"></i></div>` : ""}`
        : r.ok ? `<div class="prov-state"><span class="badge ${r.active ? "gold" : "ok"}"><span class="dot"></span>${r.active ? "сейчас работает" : "готово"}</span>${r.text ? `<span class="hint">${esc(r.text)}</span>` : ""}</div>`
          : `<div class="prov-state"><span class="badge ${r.cool ? "warn" : r.err ? "err" : ""}"><span class="dot"></span>${r.cool ? "в лимите" : r.err ? "ошибка" : "не готово"}</span><span class="hint clamp-2" title="${esc(r.text)}">${esc(r.text)}</span>${r.act || ""}</div>`;
      const omni = id === "omniroute" ? omniBlock(P) : "";
      return `<li class="prov-item${i === 0 ? " primary" : ""}" data-part="${part}" data-id="${esc(id)}">
        <div class="prov-head"><span class="prov-n num" aria-hidden="true">${i + 1}</span>
          <div class="prov-name"><b>${esc(it.label)}</b><span class="badge prov-badge ${BADGE_CLS[it.badge] || ""}">${esc(it.badge_text)}</span></div>
          <div class="prov-ctl">
            <button class="btn btn-ghost btn-sm btn-icon" type="button" data-move="-1" ${i === 0 ? "disabled" : ""} aria-label="Выше: ${esc(it.label)}">↑</button>
            <button class="btn btn-ghost btn-sm btn-icon" type="button" data-move="1" ${i === chain.length - 1 ? "disabled" : ""} aria-label="Ниже: ${esc(it.label)}">↓</button>
            <button class="btn btn-ghost btn-sm btn-icon" type="button" data-remove ${chain.length === 1 ? "disabled" : ""} aria-label="Убрать: ${esc(it.label)}">×</button>
          </div></div>
        <p class="hint prov-needs">${esc(it.needs)}</p>${optSel}${state}${omni}</li>`;
    }).join("");
    return `<section class="prov-part" aria-labelledby="pp-${part}">
      <div class="section-h"><h3 id="pp-${part}" class="grow">${title}<span class="subtle">${sub}</span></h3>${modeSel(part)}
        <button class="btn btn-sm" type="button" data-rec="${part}">Рекомендовать</button></div>
      <ol class="prov-chain" aria-label="${title}: порядок источников">${items}</ol>
      ${rest.length ? `<div class="prov-add"><select aria-label="${title}: добавить запасной источник" data-add="${part}">
        <option value="">+ добавить запасной источник…</option>${rest.map((x) => `<option value="${esc(x.id)}">${esc(x.label)} · ${esc(x.badge_text)}</option>`).join("")}</select></div>` : ""}
      ${S.recWhy?.[part] ? `<p class="hint prov-why">${esc(S.recWhy[part])}</p>` : ""}
    </section>`;
  }).join("") + `</div>`;
  $("#flowSub", box).addEventListener("change", (e) => saveProviders({ opts: { flow_subscription: e.target.checked ? "1" : "" } },
    e.target.checked ? "Flow — первый источник кадров" : "Сохранено"));
  $("#screenAck", box).addEventListener("change", async (e) => {
    if (e.target.checked && !(await confirmDlg("Включить экранный режим?", "Программа будет писать в gemini.google.com и озвучивать в AI Studio в вашем Chrome, как это делали бы вы. Это медленнее API и, вероятно, нарушает правила Google об автоматическом доступе — аккаунт могут ограничить. Проверки (вход, CAPTCHA) программа не обходит — останавливается и ждёт вас.", "Включить"))) { e.target.checked = false; return; }
    saveProviders({ opts: { screen_ack: e.target.checked ? "1" : "" } }, e.target.checked ? "Экранный режим включён" : "Экранный режим выключен");
  });
  $$("[data-mode]", box).forEach((sel) => sel.addEventListener("change", () => saveProviders({ opts: { ["mode_" + sel.dataset.mode]: sel.value } }, "Режим сохранён")));
  $$("[data-move]", box).forEach((b) => b.addEventListener("click", () => moveProv(b.closest(".prov-item"), +b.dataset.move)));
  $$("[data-remove]", box).forEach((b) => b.addEventListener("click", () => moveProv(b.closest(".prov-item"), 0)));
  $$("[data-add]", box).forEach((s) => s.addEventListener("change", () => {
    if (!s.value) return; const part = s.dataset.add;
    saveProviders({ chains: { [part]: [...S.providers.settings.chains[part], s.value] } }, "Запасной источник добавлен");
  }));
  $$("[data-opt]", box).forEach((s) => s.addEventListener("change", () => saveProviders({ opts: { [s.dataset.opt]: s.value } }, "Сохранено")));
  $$("[data-install]", box).forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await post(`/api/providers/install/${b.dataset.install}`); toast("Установка началась — прогресс виден здесь", "info", { timeout: 3000 }); } catch (e) { fail(e); b.disabled = false; }
  }));
  $$("[data-keys]", box).forEach((b) => b.addEventListener("click", () => openKeys(b.dataset.keys)));
  $$("[data-rec]", box).forEach((b) => b.addEventListener("click", () => recommendProv(b.dataset.rec, b)));
  $$("[data-test]", box).forEach((b) => b.addEventListener("click", () => testProvider(b.dataset.test, b)));
  $$("[data-sample]", box).forEach((inp) => inp.addEventListener("change", () => uploadSample(inp)));
}
function moveProv(li, dir) {
  const part = li.dataset.part, id = li.dataset.id;
  const ch = [...S.providers.settings.chains[part]], i = ch.indexOf(id);
  if (dir === 0) ch.splice(i, 1); else { const j = i + dir; if (j < 0 || j >= ch.length) return; [ch[i], ch[j]] = [ch[j], ch[i]]; }
  saveProviders({ chains: { [part]: ch } }, dir === 0 ? "Источник убран из цепочки" : "Порядок изменён");
}
async function saveProviders(body, msg) {
  try {
    S.providers = await post("/api/providers", body); renderProviders();
    const st = await api("/api/state"); S.state = st; S.usage = st.usage; renderUsage();
    if (msg) toast(msg, "success", { timeout: 1800 });
    renderTopics(); renderTopicsStatus();
  } catch (e) { fail(e); renderProviders(); }
}
async function recommendProv(part, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Подбираю…"; }
  try {
    const r = await post("/api/providers/recommend", { part: part === "all" ? null : part, apply: true });
    S.recWhy = { ...(S.recWhy || {}), ...r.why };
    if (r.snapshot) S.providers = r.snapshot;
    renderProviders();
    toast(part === "all" ? "Подобрал источники под этот компьютер" : "Цепочка подобрана под этот компьютер", "success", { timeout: 2500 });
  } catch (e) { fail(e); } finally { if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = part === "all" ? "Рекомендовать всё" : "Рекомендовать"; } }
}
async function uploadSample(inp) {
  const f = inp.files[0]; if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try {
    const r = await fetch("/api/providers/voice_sample", { method: "POST", body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(j.detail || `Ошибка ${r.status}`), { fix: j.fix });
    S.providers = j.snapshot; renderProviders(); toast(`Образец голоса сохранён (${j.seconds} с)`, "success");
  } catch (e) { fail(e); }
}
function chainLine() {
  const P = S.providers; if (!P) return "";
  return PARTS.map(([part, title]) => `${title}: ${P.settings.chains[part].map((id) => provInfo(part, id).label).join(" → ")}`).join(" · ");
}

function llmText() {
  const d = S.llm;
  if (!d || d.done || Date.now() / 1000 - (d.at || 0) > 30) return "";
  const pct = d.max_tokens ? Math.min(99, Math.round(100 * d.tokens / d.max_tokens)) : null;
  return `${d.provider === "ollama" ? "Ollama" : d.provider} · ${d.model} · ${nf.format(d.tokens)} ток.${pct != null ? ` (≤${pct}%)` : ""} · ${d.tps} ток/с · ${fmtDur(d.elapsed)}`;
}
function renderLlmProgress() {
  if (S.llm) S.llm.at = Date.now() / 1000;
  $$("[data-llm]").forEach((el) => { el.textContent = llmText(); });
}

function renderTopicsStatus() {
  const box = $("#topicsStatus"); if (!box) return;
  const st = S.topicsStatus || {};
  if (st.running) {
    box.innerHTML = `<div class="search-status"><span class="spinner" aria-hidden="true"></span><span>${esc(st.stage || "Ищу темы…")}${st.provider ? ` · ${esc(provInfo("text", st.provider).label)}` : ""}</span>
      <span class="subtle num" style="margin-left:auto" data-since="${st.started || 0}"></span></div>
      <p class="hint num" data-llm style="margin:6px 0 0">${esc(llmText())}</p>`;
  } else if (st.error) {
    const alts = st.alternatives || [];
    box.innerHTML = `<div class="alert"><b>${esc(st.error)}</b><span class="muted">${esc(st.fix || "")}</span>
      <div class="row"><button class="btn btn-sm" type="button" id="topicsRetry">Повторить поиск</button>
      ${alts.length ? `<span class="hint">Искать через другой источник:</span>${alts.map((a) => `<button class="btn btn-sm" type="button" data-alt="${esc(a.id)}">${esc(a.label)}</button>`).join("")}` : ""}
      <button class="btn btn-sm btn-ghost" type="button" id="topicsKeys">Ключи</button></div></div>`;
    $("#topicsRetry").addEventListener("click", () => refreshTopics(false));
    $("#topicsKeys").addEventListener("click", () => openKeys());
    $$("[data-alt]", box).forEach((b) => b.addEventListener("click", () => refreshTopics(false, b.dataset.alt)));
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
      grid.innerHTML = `<div class="card empty" style="grid-column:1/-1"><b>Нужен источник текста</b>
        <span>Добавьте ключ Gemini (или бесплатный ключ Groq) — либо установите Ollama в «Источниках», тогда ключи не нужны.</span>
        <div class="row" style="justify-content:center"><button class="btn btn-primary" type="button" id="emptyKeys">Добавить ключи</button>
        <button class="btn" type="button" id="emptySources">Открыть «Источники»</button></div></div>`;
      $("#emptyKeys").addEventListener("click", () => openKeys());
      $("#emptySources").addEventListener("click", () => $("#sourcesCard").scrollIntoView({ behavior: reduced ? "auto" : "smooth" }));
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
async function refreshTopics(more, provider) {
  const q = new URLSearchParams(); if (more) q.set("more", "true"); if (provider) q.set("provider", provider);
  try { await post(`/api/topics/refresh${q.toString() ? `?${q}` : ""}`); } catch (e) { fail(e); }
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
  $("#startChain").textContent = chainLine();
  updateEstimate();
  openDlg($("#dlgStart"));
}
function updateEstimate() {
  const m = +$("#startMinutes").value;
  const frames = Math.round(m * (S.state?.defaults?.wpm || 150) / 15);
  $("#startEstimate").textContent = `≈ ${nf.format(m * (S.state?.defaults?.wpm || 150))} слов, ${frames} ${plural(frames, "кадр", "кадра", "кадров")}. Ориентировочно ${fmtDur(estimateTotal(m, frames, S.providers?.settings?.chains?.images?.[0]))} работы.`;
}
$("#startMinutes").addEventListener("change", updateEstimate);
$("#startSources").addEventListener("click", () => { closeDlg($("#dlgStart")); if (S.route.name !== "home") location.hash = "#/"; setTimeout(() => $("#sourcesCard")?.scrollIntoView({ behavior: reduced ? "auto" : "smooth" }), 80); });
$("#startGo").addEventListener("click", async () => {
  const o = S.pendingStart; if (!o) return;
  const title = $("#startTitle").value.trim();
  const btn = $("#startGo"); btn.disabled = true; btn.textContent = "Запускаю…";
  try {
    const body = { target_minutes: +$("#startMinutes").value };
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
  research: 60, script: 25 + 7 * m, prompts: 10 + 1.2 * m,
  images: ({ flow: 25, pollinations: 17, comfyui: 12, hf: 8, none: 0.2 }[backend] ?? 3) * frames + 20,
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
  $("#prodMeta").textContent = `${m} мин · кадры: ${provInfo("images", snap.image_backend || "gemini_api").label}`;
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
  $("#errKeys") && $("#errKeys").addEventListener("click", () => openKeys());
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
const EXTRA_KEYS = [
  ["GROQ_API_KEY", "Groq", "бесплатно ≈1 000 запросов/день к Llama 3.3 70B", "https://console.groq.com/keys"],
  ["OPENROUTER_API_KEY", "OpenRouter", "бесплатные модели: 50 запросов/день (1 000 после пополнения на $10)", "https://openrouter.ai/keys"],
  ["MISTRAL_API_KEY", "Mistral", "бесплатный тариф Experiment", "https://console.mistral.ai/api-keys"],
  ["CEREBRAS_API_KEY", "Cerebras", "бесплатный пробный тариф", "https://cloud.cerebras.ai"],
  ["OMNIROUTE_API_KEY", "OmniRoute — ключ шлюза", "необязательно: нужен, только если в OmniRoute включена защита ключом или для полного списка моделей", "http://localhost:20128/dashboard"],
  ["OMNIROUTE_URL", "OmniRoute — адрес", "необязательно: если OmniRoute не на http://localhost:20128", "http://localhost:20128/dashboard"],
  ["CUSTOM_LLM_URL", "Свой OpenAI-совместимый сервер — адрес", "LM Studio, vLLM, llama.cpp, прокси: например http://localhost:1234/v1", "https://lmstudio.ai"],
  ["CUSTOM_LLM_KEY", "Свой сервер — ключ", "если сервер требует ключ", "https://lmstudio.ai"],
  ["CUSTOM_LLM_MODEL", "Свой сервер — модель", "имя модели на вашем сервере", "https://lmstudio.ai"],
  ["DEEPSEEK_API_KEY", "DeepSeek", "платно, очень дёшево", "https://platform.deepseek.com/api_keys"],
  ["XAI_API_KEY", "xAI Grok", "по тарифу xAI", "https://console.x.ai"],
  ["OPENAI_API_KEY", "OpenAI", "платно", "https://platform.openai.com/api-keys"],
  ["GEMINI_PAID_API_KEY", "Gemini с оплатой", "ключ проекта с включённым биллингом — без дневных лимитов, платно", "https://aistudio.google.com/apikey"],
  ["HF_TOKEN", "Hugging Face", "кадры FLUX через бесплатные кредиты", "https://huggingface.co/settings/tokens"],
  ["POLLINATIONS_TOKEN", "Pollinations", "необязательно: ускоряет бесплатные кадры", "https://auth.pollinations.ai"],
  ["YOUTUBE_API_KEY", "YouTube Data API", "необязательно: видео канала и конкурентов для тем", "https://console.cloud.google.com/apis/library/youtube.googleapis.com"],
];
function renderKeysList() {
  const k = S.keys || { keys: [] };
  $("#keysSummary").innerHTML = `<span class="badge ${k.invalid ? "warn" : "ok"}">${esc(k.text || "")}</span>${k.next_reset ? `<span class="hint">ближайший сброс квоты ≈ ${tf.format(new Date(k.next_reset * 1000))}</span>` : ""}`;
  $("#keysList").innerHTML = (k.keys || []).map((x) => `<li><span class="subtle num">№${x.index}</span><code class="truncate" translate="no">${esc(x.mask)}</code>
    <input class="key-proj" data-fp="${esc(x.fp || "")}" value="${esc(x.project || "")}" placeholder="проект" aria-label="Проект Google для ключа №${x.index}" autocomplete="off" spellcheck="false">
    ${x.status === "ok" ? `<span class="badge ok">работает</span>` : x.status === "quota" ? `<span class="badge warn">квота до ${x.until ? tf.format(new Date(x.until * 1000)) : "…"}</span>` :
      x.status === "invalid" ? `<span class="badge err" title="${esc(x.reason)}">неверный</span>` : `<span class="badge">не проверен</span>`}</li>`).join("") || `<li class="empty-li"><span class="muted">Ключей пока нет</span></li>`;
  $("#keysRemoveBad").disabled = !k.invalid;
  $$(".key-proj").forEach((inp) => inp.addEventListener("change", saveProjects));
  const sec = S.providers?.secrets || {};
  $("#extraKeys").innerHTML = EXTRA_KEYS.map(([id, name, what, url]) => `<div class="field extra-key">
    <label for="xk-${id}">${esc(name)} ${sec[id] ? `<span class="badge ok">сохранён</span>` : ""}</label>
    <input id="xk-${id}" data-secret="${id}" type="${/_(URL|MODEL)$/.test(id) ? "text" : "password"}" autocomplete="off" spellcheck="false" placeholder="${sec[id] ? "•••••••• (оставьте пустым, чтобы не менять)" : "вставьте ключ…"}">
    <span class="hint">${esc(what)} · <a href="${url}" target="_blank" rel="noopener noreferrer">где взять</a>${sec[id] ? ` · <button class="linkbtn" type="button" data-clear="${id}">удалить</button>` : ""}</span></div>`).join("");
  $$("[data-clear]").forEach((b) => b.addEventListener("click", async () => {
    try { const r = await post("/api/keys/extra", { values: { [b.dataset.clear]: "" } }); S.providers.secrets = r.secrets; renderKeysList(); renderProviders(); toast("Ключ удалён", "success"); } catch (e) { fail(e); }
  }));
}
async function saveProjects() {
  const labels = Object.fromEntries($$(".key-proj").filter((i) => i.dataset.fp).map((i) => [i.dataset.fp, i.value.trim()]));
  try { const r = await post("/api/keys/projects", { labels }); S.keys = r.keys; S.usage = r.usage; renderUsage(); toast("Проекты ключей сохранены", "success", { timeout: 1800 }); } catch (e) { fail(e); }
}
function openKeys(focus) {
  renderKeysList(); openDlg($("#dlgKeys"));
  const el = typeof focus === "string" && focus !== "GEMINI_API_KEY" ? $(`#xk-${focus}`) : $("#keysText");
  setTimeout(() => { el?.focus(); if (el && el !== $("#keysText")) el.scrollIntoView({ block: "center" }); }, 60);
}
$("#keysPill").addEventListener("click", () => openKeys());
$("#sourcesBtn").addEventListener("click", () => {
  if (S.route.name !== "home") location.hash = "#/";
  setTimeout(() => $("#sourcesCard")?.scrollIntoView({ behavior: reduced ? "auto" : "smooth" }), 80);
});
$("#keysSave").addEventListener("click", async () => {
  const btn = $("#keysSave"); btn.disabled = true; btn.textContent = "Сохраняю…";
  try {
    const extra = Object.fromEntries($$("[data-secret]").filter((i) => i.value.trim()).map((i) => [i.dataset.secret, i.value.trim()]));
    if (Object.keys(extra).length) { const r = await post("/api/keys/extra", { values: extra }); if (S.providers) S.providers.secrets = r.secrets; }
    if ($("#keysText").value.trim() || $("#keysReplace").checked) {
      btn.textContent = "Проверяю ключи…";
      S.keys = await post("/api/keys", { text: $("#keysText").value, replace: $("#keysReplace").checked });
      $("#keysText").value = ""; $("#keysReplace").checked = false;
      toast(`Ключи сохранены: ${S.keys.text}`, "success");
    } else if (Object.keys(extra).length) toast("Ключи сохранены", "success");
    renderKeysPill(); renderKeysList(); renderProviders();
    const st = await api("/api/state"); S.state = st; S.providers = st.providers; if (S.route.name === "home") { renderTopics(); renderTopicsStatus(); renderProviders(); }
  } catch (e) { fail(e); } finally { btn.disabled = false; btn.textContent = "Сохранить"; }
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
  on("usage", (d) => { S.usage = d; renderUsage(); });
  on("providers", (d) => { S.providers = d; renderProviders(); });
  ["text", "voice", "images"].forEach((part) => on(`route_${part}`, (d) => { if (S.providers) { S.providers.routes[part] = d; renderProviders(); } }));
  on("health", (d) => { S.health = d; renderHealth(); });
  on("toast", (d) => toast(d.message, d.level, { fix: d.fix }));
  on("frame", onFrame);
  on("voice", (d) => { if (S.project && d.project === S.project.id) { S.waveFor = null; loadWave(d.project, false); } });
  es.addEventListener("open", () => { if (S.lostConn) { S.lostConn = false; boot(true); } });
  on("llm_progress", (d) => { S.llm = d; renderLlmProgress(); });
  es.addEventListener("error", () => { S.lostConn = true; });
}

function showConnecting(attempt, why) {
  if (S.state) return;  // уже подключались — не прячем интерфейс, SSE переподключится сам
  $("#view").innerHTML = `<section class="card card-pad connecting" role="status" aria-live="polite">
    <span class="spinner" aria-hidden="true"></span>
    <div><b>ИСТОРИК FACTORY запускается…</b>
    <div class="hint">Подключаюсь к программе${attempt > 1 ? ` · попытка ${attempt}` : ""}${why ? ` · ${esc(why)}` : ""}</div></div></section>`;
}

async function boot(again, attempt = 1) {
  let st;
  try {
    st = await api("/api/state", { timeout: 8000 });
  } catch (e) {  // сервер ещё стартует или занят — повторяем с нарастающей паузой, а не бросаем пустую страницу
    showConnecting(attempt, e.message);
    const delay = Math.min(10000, 500 * 2 ** Math.min(attempt, 5));
    setTimeout(() => boot(again, attempt + 1), delay);
    return false;
  }
  S.state = st; S.stages = st.stages; S.keys = st.keys; S.health = st.health; S.topicsStatus = st.topics_status || {};
  S.usage = st.usage; S.providers = st.providers;
  $("#channelLine").textContent = `канал «${st.channel || "ИСТОРИК"}»${st.mock ? " · тестовый режим" : ""}`;
  renderKeysPill();
  S.route = parseRoute();
  if (!again || attempt > 1) render();
  await Promise.all([loadTopics(), loadProjects()]);
  if (again && S.route.name === "project") renderProjectView(S.route.id);
  if (!again && S.state.missing_secrets.length) setTimeout(() => openKeys(), 400);
  if (!S.connected) { S.connected = true; connect(); }
  return true;
}

showConnecting(1);
boot(false);

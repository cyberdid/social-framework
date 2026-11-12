"use strict";
/* Social web app — no build step, no dependencies. */

// ---------------------------------------------------------------- token & api
(function initToken() {
  const m = location.hash.match(/[#?&]t=([\w-]+)/);
  if (m) {
    try { sessionStorage.setItem("socialToken", m[1]); } catch (e) { window.__tok = m[1]; }
    const rest = location.hash.replace(/[#?&]t=[\w-]+/, "").replace(/^#?$/, "");
    history.replaceState(null, "", rest && rest !== "#" ? rest : "#/");
  }
})();
const TOKEN = (() => { try { return sessionStorage.getItem("socialToken") || window.__tok; } catch (e) { return window.__tok; } })();

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "X-Social-Token": TOKEN || "", "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (r.status === 401) throw new Error("Немає доступу: відкрийте посилання з ключем, яке надрукувала команда «social web».");
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}
const fileUrl = (slug, path) => `/api/cases/${encodeURIComponent(slug)}/file?path=${encodeURIComponent(path)}&t=${encodeURIComponent(TOKEN || "")}`;

// ---------------------------------------------------------------- helpers
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (p) => `${Math.round(p * 100)} %`;
const plural = (n, one, few, many) => { const a = n % 10, b = n % 100; return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 10 || b >= 20) ? few : many; };
function ago(ts) {
  const t = typeof ts === "number" ? ts * 1000 : Date.parse(ts); if (!t) return "";
  const s = (Date.now() - t) / 1000;
  if (s < 60) return "щойно"; if (s < 3600) return `${Math.floor(s / 60)} хв тому`; if (s < 86400) return `${Math.floor(s / 3600)} год тому`;
  return new Date(t).toLocaleDateString("uk-UA", { day: "numeric", month: "short" });
}
function toast(msg, ms = 3200) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms); }
const fmtDate = (d) => d ? new Date(d).toLocaleDateString("uk-UA", { day: "numeric", month: "short", year: "numeric" }) : "";
const linkify = (s) => esc(s).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');

const AGENT_STYLE = {
  claude: { c: "linear-gradient(135deg,#d97757,#b4532a)", l: "C" },
  codex: { c: "linear-gradient(135deg,#2b2f36,#10a37f)", l: "X" },
  agy: { c: "linear-gradient(135deg,#4285f4,#34a853)", l: "G" },
  opencode: { c: "linear-gradient(135deg,#6250d6,#2f2a4a)", l: "O" },
};
const agentMark = (name, size = 30) => { const s = AGENT_STYLE[name] || { c: "var(--grad)", l: "?" };
  return `<span class="avatar" style="background:${s.c};width:${size}px;height:${size}px">${s.l}</span>`; };

// verdicts: fixed order, status colors, always with a text label
const VERDICTS = [
  ["правда", "var(--good)"], ["ймовірно правда", "color-mix(in srgb, var(--good) 65%, var(--muted))"],
  ["частково", "var(--warning)"], ["перекручено", "var(--serious)"], ["не підтверджено", "var(--muted)"],
  ["припущення", "color-mix(in srgb, var(--muted) 70%, var(--text-2))"], ["неправда", "var(--critical)"], ["фейк", "color-mix(in srgb, var(--critical) 80%, #000)"],
];
const VCOLOR = Object.fromEntries(VERDICTS);
const LEVEL_TXT = { A: "документ", B: "кілька джерел", C: "одне джерело", D: "чутки" };
const SCOL = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--muted)"];

// ---------------------------------------------------------------- state & routing
const S = { meta: null, cases: [], slug: null, tab: "chat", chat: null, streaming: null };
const TABS = [["chat", "Чат"], ["overview", "Огляд"], ["facts", "Фактчеки"], ["judgments", "Висновки"], ["scenarios", "Сценарії"], ["data", "Дані"], ["report", "Звіт"], ["log", "Журнал"]];

function route() {
  const m = location.hash.match(/^#\/c\/([^/]+)(?:\/(\w+))?(?:\/([\w-]+))?/);
  if (m) { S.slug = decodeURIComponent(m[1]); S.tab = m[2] || "chat"; S.chat = m[3] || null; }
  else { S.slug = null; }
  $("#sidebar").classList.remove("open");
  renderSidebar(); renderView();
}
const go = (h) => { if (location.hash === h) route(); else location.hash = h; };
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- theme
function setTheme(t) { document.documentElement.dataset.theme = t; try { localStorage.setItem("socialTheme", t); } catch (e) {} $("#themeBtn").innerHTML = `<span class="i-${t === "dark" ? "sun" : "moon"}"></span>`; }
(() => { let t = null; try { t = localStorage.getItem("socialTheme"); } catch (e) {} setTheme(t || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")); })();
$("#themeBtn").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
$("#menuBtn").onclick = () => $("#sidebar").classList.toggle("open");

// ---------------------------------------------------------------- sidebar
async function loadCases() { S.cases = await api("/api/cases"); renderSidebar(); }
function renderSidebar() {
  const el = $("#caseList");
  if (!S.cases.length) { el.innerHTML = `<div class="sub" style="padding:10px 12px;color:var(--muted);font-size:13px">Кейсів ще немає</div>`; return; }
  el.innerHTML = S.cases.map((c) => `<a class="case-item ${c.slug === S.slug ? "active" : ""}" href="#/c/${encodeURIComponent(c.slug)}">
    <div class="t">${esc(c.title)}</div><div class="m"><span>${c.factchecks} фактч.</span><span>${c.judgments} висн.</span><span>${ago(c.updated)}</span></div></a>`).join("");
}
$("#newCaseBtn").onclick = () => newCaseModal();

// ---------------------------------------------------------------- modal
function modal(html, onSubmit) {
  const d = $("#modal"), b = $("#modalBody");
  b.innerHTML = html; d.showModal();
  b.onsubmit = async (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    const btn = b.querySelector("button[value=ok]"); if (btn) btn.disabled = true;
    try { await onSubmit(new FormData(b)); d.close(); } catch (err) { toast(err.message, 5000); } finally { if (btn) btn.disabled = false; }
  };
  setTimeout(() => { const f = b.querySelector("input,textarea,select"); f && f.focus(); }, 30);
}
function newCaseModal() {
  modal(`<h2>Новий кейс</h2><p class="d">Опишіть, що хочете з'ясувати. Агент почне з цього і уточнить решту в розмові.</p>
    <label class="f">Назва<input class="inp" name="title" required placeholder="Наприклад: Операція «…» в інформаційному просторі"></label>
    <label class="f">Питання<textarea class="inp" name="question" placeholder="Що саме хочемо зрозуміти? Для кого довідка?"></textarea></label>
    <div class="actions"><button class="btn" value="cancel" formnovalidate>Скасувати</button><button class="btn primary" value="ok">Створити</button></div>`,
  async (f) => { const c = await api("/api/cases", { method: "POST", body: JSON.stringify({ title: f.get("title"), question: f.get("question") }) });
    await loadCases(); go(`#/c/${encodeURIComponent(c.slug)}/chat`); toast("Кейс створено"); });
}

async function modelOptions(agent, current) {
  let ms = [];
  try { ms = (await api(`/api/agents/${agent}/models`)).models; } catch (e) {}
  const opts = [`<option value="">модель агента за замовчуванням</option>`, ...ms.map((m) => `<option ${m === current ? "selected" : ""}>${esc(m)}</option>`)];
  if (current && !ms.includes(current)) opts.push(`<option selected>${esc(current)}</option>`);
  return opts.join("");
}
function agentSelect(name, current) {
  return `<select class="inp" name="${name}">${S.meta.agents.map((a) => `<option value="${a.name}" ${a.name === current ? "selected" : ""} ${a.installed ? "" : "disabled"}>${esc(a.title)}${a.installed ? "" : " — не встановлено"}</option>`).join("")}</select>`;
}
async function wireModelPicker(root, agentName, modelName, current) {
  const sel = root.querySelector(`[name=${agentName}]`), msel = root.querySelector(`[name=${modelName}]`);
  const load = async (cur) => { msel.innerHTML = `<option>завантаження моделей…</option>`; msel.innerHTML = await modelOptions(sel.value, cur); };
  sel.onchange = () => load(""); await load(current);
}
$("#settingsBtn").onclick = async () => {
  const c = S.meta.config;
  modal(`<h2>Агенти й моделі</h2><p class="d">Хто працює аналітиком і хто — червоною командою. Найкраще, коли рецензент від іншого виробника.</p>
    <div class="row2"><label class="f">Аналітик${agentSelect("aa", c.analyst.agent)}</label><label class="f">Модель<select class="inp" name="am"></select></label></div>
    <div class="row2"><label class="f">Червона команда${agentSelect("ra", S.meta.reviewer[0])}</label><label class="f">Модель<select class="inp" name="rm"></select></label></div>
    <div class="actions"><button class="btn" value="cancel" formnovalidate>Скасувати</button><button class="btn primary" value="ok">Зберегти</button></div>`,
  async (f) => { await api("/api/config", { method: "PUT", body: JSON.stringify({ analyst: { agent: f.get("aa"), model: f.get("am") }, reviewer: { agent: f.get("ra"), model: f.get("rm") } }) });
    S.meta = await api("/api/meta"); toast("Збережено"); });
  const b = $("#modalBody");
  wireModelPicker(b, "aa", "am", c.analyst.model); wireModelPicker(b, "ra", "rm", S.meta.reviewer[1]);
};

// ---------------------------------------------------------------- views
const view = () => $("#view");
function renderView() {
  view().classList.remove("chat-mode");
  if (!S.slug) return renderHome();
  const c = S.cases.find((x) => x.slug === S.slug);
  if (!c) { view().innerHTML = `<div class="empty"><b>Кейс не знайдено</b><a href="#/">До всіх кейсів</a></div>`; return; }
  $("#crumbs").innerHTML = `<a href="#/">Кейси</a><span>/</span><b>${esc(c.title)}</b>`;
  const counts = { facts: c.factchecks, judgments: c.judgments, scenarios: c.scenarios, chat: c.chats };
  view().innerHTML = `<div class="case-head"><div><h1>${esc(c.title)}</h1>${c.question ? `<div class="q">${esc(c.question)}</div>` : ""}</div>
      <div class="head-actions"><button class="btn" id="reviewBtn"><span class="i-shield"></span>Червона команда</button><button class="btn" id="buildBtn"><span class="i-doc"></span>Зібрати звіт</button></div></div>
    <div class="tabs" role="tablist">${TABS.map(([k, l]) => `<button class="tab ${k === S.tab ? "active" : ""}" data-k="${k}">${l}${counts[k] ? `<span class="n">${counts[k]}</span>` : ""}</button>`).join("")}</div>
    <div id="tabBody"></div>`;
  view().querySelectorAll(".tab").forEach((t) => (t.onclick = () => go(`#/c/${encodeURIComponent(S.slug)}/${t.dataset.k}`)));
  $("#reviewBtn").onclick = startReview;
  $("#buildBtn").onclick = buildReport;
  ({ chat: renderChat, overview: renderOverview, facts: renderFacts, judgments: renderJudgments, scenarios: renderScenarios, data: renderData, report: renderReport, log: renderLog }[S.tab] || renderChat)();
}
const body = () => $("#tabBody");
const skeleton = (n = 3) => `<div class="grid g3">${Array.from({ length: n }, () => '<div class="skeleton"></div>').join("")}</div>`;

function renderHome() {
  $("#crumbs").innerHTML = `<b>Кейси</b>`;
  const agents = S.meta.agents.map((a) => `<div class="card agent-card">${agentMark(a.name, 38)}<div><div class="n">${esc(a.title.split(" (")[0])}</div>
      <div class="s ${a.installed ? "ok" : "no"}">${a.installed ? "● готовий" : "не встановлено"}</div></div></div>`).join("");
  const cases = S.cases.map((c) => `<a class="card hover" href="#/c/${encodeURIComponent(c.slug)}" style="color:inherit;text-decoration:none">
      <h3>${esc(c.title)}</h3><div class="sub" style="min-height:40px">${esc(c.question || "Питання ще не записано")}</div>
      <div class="row" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:12px"><span class="chip">${c.factchecks} фактчеків</span><span class="chip">${c.judgments} висновків</span>${c.scenarios ? '<span class="chip">сценарії</span>' : ""}<span class="chip">${ago(c.updated)}</span></div></a>`).join("");
  view().innerHTML = `<div class="hero"><h1>Розслідуйте інформаційний простір<br><span class="g">з агентом на ваш вибір</span></h1>
      <p>Claude, Codex, Gemini чи відкриті моделі збирають публічні сліди, перевіряють твердження, роблять висновки з ймовірностями й будують сценарії. Усе зберігається у вашому кейсі.</p></div>
    <div class="section-title"><h2>Агенти</h2><button class="ghost-btn" id="cfgBtn2"><span class="i-gear"></span>Налаштувати</button></div>
    <div class="grid g4">${agents}</div>
    <div class="section-title"><h2>Кейси</h2><button class="btn primary" id="nc2"><span class="i-plus"></span>Новий кейс</button></div>
    ${S.cases.length ? `<div class="grid g3">${cases}</div>` : `<div class="empty"><b>Почніть перший кейс</b>Опишіть подію чи питання — агент допоможе спланувати збір і перевірку.</div>`}`;
  $("#nc2").onclick = newCaseModal; $("#cfgBtn2").onclick = () => $("#settingsBtn").click();
}

// ---------------------------------------------------------------- chat
async function renderChat() {
  view().classList.add("chat-mode");
  body().style.cssText = "flex:1;min-height:0;display:flex";
  body().innerHTML = `<div class="chat"><div class="chat-list" id="chatList"><button class="btn wide" id="newChat"><span class="i-plus"></span>Нова розмова</button><div class="items" id="chatItems"></div></div>
    <div class="chat-pane"><div class="chat-top" id="chatTop"></div><div class="msgs" id="msgs"></div>
    <div class="composer"><div class="composer-box"><textarea id="inp" rows="1" placeholder="Напишіть аналітику… (Enter — надіслати, Shift+Enter — новий рядок)"></textarea>
    <button class="send" id="sendBtn" title="Надіслати"><span class="i-send"></span></button></div><div class="meta"><span id="agentMeta"></span><span>Відповіді й дії агента зберігаються в кейсі</span></div></div></div></div>`;
  $("#newChat").onclick = () => newChatModal();
  const inp = $("#inp");
  inp.addEventListener("input", () => { inp.style.height = "auto"; inp.style.height = Math.min(inp.scrollHeight, 220) + "px"; });
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMsg(); } });
  $("#sendBtn").onclick = () => (S.streaming ? stopMsg() : sendMsg());
  const chats = await api(`/api/cases/${encodeURIComponent(S.slug)}/chats`);
  renderChatList(chats);
  if (!S.chat && chats.length) S.chat = chats[0].id;
  if (S.chat) await openChat(S.chat); else renderWelcome();
}
function renderChatList(chats) {
  $("#chatItems").innerHTML = chats.map((c) => `<div class="chat-item ${c.id === S.chat ? "active" : ""}" data-id="${c.id}">
    <div class="t">${c.role === "reviewer" ? "🛡 " : ""}${esc(c.title)}</div><div class="m">${agentMark(c.agent, 16)}<span>${esc(c.agent)}${c.model ? " · " + esc(c.model) : ""}</span><span>· ${ago(c.updated)}</span></div></div>`).join("");
  $("#chatItems").querySelectorAll(".chat-item").forEach((el) => (el.onclick = () => go(`#/c/${encodeURIComponent(S.slug)}/chat/${el.dataset.id}`)));
}
function renderWelcome() {
  $("#chatTop").innerHTML = `<span class="sub" style="color:var(--muted)">Нова розмова</span>`;
  $("#agentMeta").textContent = `${S.meta.config.analyst.agent} · ${S.meta.config.analyst.model || "модель за замовчуванням"}`;
  const sug = ["Подивись стан кейсу і запропонуй план", "Збери Telegram і новини за останній тиждень", "Перевір найабсурдніші твердження", "Сформулюй сценарії розвитку з ймовірностями"];
  $("#msgs").innerHTML = `<div class="welcome"><span class="logo" style="display:inline-block"></span><h2>З чого почнемо?</h2><p>Аналітик бачить усі файли кейсу та інструменти Social: збір, фактчеки, висновки, сценарії, звіт.</p>
    <div class="suggest">${sug.map((s) => `<button>${esc(s)}</button>`).join("")}</div></div>`;
  $("#msgs").querySelectorAll(".suggest button").forEach((b) => (b.onclick = () => { $("#inp").value = b.textContent; sendMsg(); }));
}
async function newChatModal(role = "analyst") {
  const cfg = role === "reviewer" ? { agent: S.meta.reviewer[0], model: S.meta.reviewer[1] } : S.meta.config.analyst;
  modal(`<h2>${role === "reviewer" ? "Червона команда" : "Нова розмова"}</h2><p class="d">${role === "reviewer" ? "Інший агент читає кейс і шукає слабкі місця. Змінювати нічого не може." : "Оберіть агента й модель. Їх можна змінювати від розмови до розмови: кейс той самий."}</p>
    <div class="row2"><label class="f">Агент${agentSelect("ag", cfg.agent)}</label><label class="f">Модель<select class="inp" name="md"></select></label></div>
    <div class="actions"><button class="btn" value="cancel" formnovalidate>Скасувати</button><button class="btn primary" value="ok">${role === "reviewer" ? "Почати рецензію" : "Почати"}</button></div>`,
  async (f) => { const c = await api(`/api/cases/${encodeURIComponent(S.slug)}/chats`, { method: "POST", body: JSON.stringify({ agent: f.get("ag"), model: f.get("md"), role }) });
    S.chat = c.id; history.replaceState(null, "", `#/c/${encodeURIComponent(S.slug)}/chat/${c.id}`);
    if (S.tab !== "chat") { S.tab = "chat"; renderView(); await new Promise((r) => setTimeout(r, 50)); } else await renderChat();
    if (role === "reviewer") sendMsg("Проведи рецензію кейсу як червона команда.", true); });
  wireModelPicker($("#modalBody"), "ag", "md", cfg.model);
}
async function openChat(id) {
  S.chat = id;
  const rows = await api(`/api/cases/${encodeURIComponent(S.slug)}/chats/${id}`);
  const meta = rows[0];
  $("#chatTop").innerHTML = `<div style="display:flex;gap:10px;align-items:center;min-width:0">${agentMark(meta.agent, 26)}<div style="min-width:0"><div style="font-weight:600;font-size:14px">${esc(meta.agent)}${meta.role_name === "reviewer" ? " · червона команда" : ""}</div><div class="sub" style="color:var(--muted);font-size:12px">${esc(meta.model || "модель за замовчуванням")}</div></div></div><span class="chip">${rows.filter((r) => r.role === "user").length} ${plural(rows.filter((r) => r.role === "user").length, "запит", "запити", "запитів")}</span>`;
  $("#agentMeta").textContent = `${meta.agent} · ${meta.model || "модель за замовчуванням"}`;
  const msgs = $("#msgs"); msgs.innerHTML = ""; msgs.classList.add("static");
  for (const r of rows.slice(1)) {
    if (r.role === "user") addUser(r.text);
    else if (r.role === "assistant") { const b = addBot(meta.agent); fillBot(b, r); }
  }
  if (rows.length === 1) renderWelcome();
  msgs.scrollTop = msgs.scrollHeight;
  document.querySelectorAll(".chat-item").forEach((el) => el.classList.toggle("active", el.dataset.id === id));
}
function addUser(text) {
  const w = $(".welcome"); if (w) w.remove();
  const el = document.createElement("div"); el.className = "msg user";
  el.innerHTML = `<span class="avatar">Ви</span><div class="bubble">${esc(text.length > 1500 ? text.slice(0, 1500) + "…" : text)}</div>`;
  $("#msgs").appendChild(el); return el;
}
function addBot(agent) {
  const el = document.createElement("div"); el.className = "msg bot";
  el.innerHTML = `${agentMark(agent)}<div class="body"><div class="who">${esc(agent)}</div><div class="tools"></div><div class="prose"></div></div>`;
  $("#msgs").appendChild(el); return el;
}
const toolChip = (t, live) => `<span class="tool-chip ${live ? "live" : ""}"><span class="i-tool"></span>${esc(t.name)}${t.detail ? `<span class="d">${esc(t.detail)}</span>` : ""}</span>`;
function fillBot(el, r) {
  $(".tools", el).innerHTML = (r.tools || []).map((t) => toolChip(t)).join("");
  $(".prose", el).innerHTML = (r.html || esc(r.text)) + (r.error ? `<div class="err">${esc(r.error)}</div>` : "");
}
async function sendMsg(text, review = false) {
  const inp = $("#inp"); text = text || inp.value.trim();
  if (!text || S.streaming) return;
  if (!S.chat) {
    const c = await api(`/api/cases/${encodeURIComponent(S.slug)}/chats`, { method: "POST", body: JSON.stringify({}) });
    S.chat = c.id; history.replaceState(null, "", `#/c/${encodeURIComponent(S.slug)}/chat/${c.id}`);
    const rows = await api(`/api/cases/${encodeURIComponent(S.slug)}/chats/${c.id}`); $("#msgs").innerHTML = "";
    $("#chatTop").innerHTML = `<div style="display:flex;gap:10px;align-items:center">${agentMark(rows[0].agent, 26)}<b style="font-size:14px">${esc(rows[0].agent)}</b><span class="sub" style="color:var(--muted);font-size:12px">${esc(rows[0].model || "")}</span></div>`;
  }
  inp.value = ""; inp.style.height = "auto";
  $("#msgs").classList.remove("static");
  addUser(text);
  const agent = ($("#chatTop .avatar") && $("#agentMeta").textContent.split(" · ")[0]) || "";
  const bot = addBot(agent); const prose = $(".prose", bot), tools = $(".tools", bot);
  prose.innerHTML = `<span class="typing"><i></i><i></i><i></i></span>`;
  const msgs = $("#msgs"); msgs.scrollTop = msgs.scrollHeight;
  const ctrl = new AbortController(); S.streaming = ctrl; setSendState(true);
  let txt = "";
  try {
    const r = await fetch(`/api/cases/${encodeURIComponent(S.slug)}/chats/${S.chat}/send`, { method: "POST", signal: ctrl.signal,
      headers: { "X-Social-Token": TOKEN || "", "Content-Type": "application/json" }, body: JSON.stringify({ text, review }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!chunk.startsWith("data: ")) continue;
        const e = JSON.parse(chunk.slice(6));
        if (e.t === "text") { txt += (txt ? "\n\n" : "") + e.text; prose.innerHTML = `<p style="white-space:pre-wrap">${esc(txt)}</p><span class="typing"><i></i><i></i><i></i></span>`; }
        else if (e.t === "tool") { tools.insertAdjacentHTML("beforeend", toolChip(e, true)); }
        else if (e.t === "error") { prose.insertAdjacentHTML("beforeend", `<div class="err">${esc(e.text)}</div>`); }
        else if (e.t === "done") { prose.innerHTML = e.html || esc(e.text); tools.querySelectorAll(".live").forEach((x) => x.classList.remove("live")); }
        msgs.scrollTop = msgs.scrollHeight;
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") prose.insertAdjacentHTML("beforeend", `<div class="err">${esc(err.message)}</div>`);
  } finally {
    const ty = prose.querySelector(".typing"); if (ty) ty.remove();
    S.streaming = null; setSendState(false);
    api(`/api/cases/${encodeURIComponent(S.slug)}/chats`).then(renderChatList).catch(() => {});
    loadCases();
  }
}
async function stopMsg() {
  if (!S.streaming) return;
  await api(`/api/cases/${encodeURIComponent(S.slug)}/chats/${S.chat}/stop`, { method: "POST" }).catch(() => {});
  toast("Зупинено");
}
function setSendState(on) { const b = $("#sendBtn"); if (!b) return; b.classList.toggle("stop", on); b.innerHTML = `<span class="i-${on ? "stop" : "send"}"></span>`; b.title = on ? "Зупинити" : "Надіслати"; }
function startReview() { newChatModal("reviewer"); }

// ---------------------------------------------------------------- overview
async function renderOverview() {
  body().innerHTML = skeleton(4);
  const st = await api(`/api/cases/${encodeURIComponent(S.slug)}/status`);
  const tiles = [["Фактчеки", st.factchecks], ["Висновки", st.judgments], ["Знімки даних", st.raw_snapshots.length], ["Фіксації", st.precommits.length]];
  body().innerHTML = `<div class="grid g4">${tiles.map(([l, v]) => `<div class="card stat"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("")}</div>
    ${st.recommendations.length ? `<div class="section-title"><h2>Що варто зробити далі</h2></div>${st.recommendations.map((h) => `<div class="hint">${esc(h)}</div>`).join("")}` : ""}
    <div class="grid g2" style="margin-top:22px">
      <div class="card"><h3>Фонові збори</h3>${st.jobs.length ? st.jobs.map((j) => `<div style="display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px solid var(--line)"><span class="mono">${esc(j.id)}</span>${jobPill(j)}</div>`).join("") : `<div class="sub">Поки немає. Агент запускає їх сам, коли треба зібрати дані.</div>`}</div>
      <div class="card"><h3>Останні дії</h3><div class="loglines"><ul>${st.log_tail.slice(-8).reverse().map((l) => `<li>${esc(l.replace(/^- /, "").replace(/^(\S+)T(\d\d:\d\d)\S*/, "$1 $2"))}</li>`).join("")}</ul></div></div>
    </div>
    <div class="card" style="margin-top:16px"><h3>Опис кейсу</h3><pre class="mono" style="white-space:pre-wrap;margin:0;color:var(--text-2)">${esc(JSON.stringify(st.case_yaml, null, 2))}</pre></div>`;
}
const jobPill = (j) => j.state === "running" ? `<span class="pill run">триває</span>` : j.state === "finished" && j.exit_code === 0 ? `<span class="pill fin">готово</span>` : `<span class="pill bad">${j.exit_code != null ? "помилка " + j.exit_code : "перервано"}</span>`;

// ---------------------------------------------------------------- fact-checks
async function renderFacts() {
  body().innerHTML = skeleton(6);
  const fc = await api(`/api/cases/${encodeURIComponent(S.slug)}/records/factchecks`);
  if (!fc.length) { body().innerHTML = `<div class="empty"><b>Фактчеків ще немає</b>Попросіть аналітика перевірити найгучніші чи найабсурдніші твердження.</div>`; return; }
  const counts = Object.fromEntries(VERDICTS.map(([v]) => [v, fc.filter((f) => f.verdict === v).length]));
  let filter = null;
  const draw = () => {
    const list = filter ? fc.filter((f) => f.verdict === filter) : fc;
    body().innerHTML = `<div class="card" style="margin-bottom:16px"><h3>Розподіл вердиктів · ${fc.length} ${plural(fc.length, "твердження", "твердження", "тверджень")}</h3>
      <div class="stack">${VERDICTS.filter(([v]) => counts[v]).map(([v, c]) => `<div style="flex:${counts[v]};background:${c}" title="${esc(v)}: ${counts[v]}">${counts[v] / fc.length > 0.07 ? counts[v] : ""}</div>`).join("")}</div>
      <div class="legend" style="margin-top:12px">${VERDICTS.filter(([v]) => counts[v]).map(([v, c]) => `<span><i style="background:${c}"></i>${esc(v)} · ${counts[v]}</span>`).join("")}</div></div>
      <div class="filters"><button class="${!filter ? "on" : ""}" data-v="">Усі · ${fc.length}</button>${VERDICTS.filter(([v]) => counts[v]).map(([v, c]) => `<button class="${filter === v ? "on" : ""}" data-v="${esc(v)}"><i style="width:8px;height:8px;border-radius:50%;background:${c};display:inline-block"></i>${esc(v)}</button>`).join("")}</div>
      <div class="fc-grid">${list.map((f) => `<div class="card fc"><div class="row"><span class="badge"><span class="dot" style="background:${VCOLOR[f.verdict] || "var(--muted)"}"></span>${esc(f.verdict)}</span><span class="lvl" title="${esc(LEVEL_TXT[f.level] || "")}">${esc(f.level)} · ${esc(LEVEL_TXT[f.level] || "")}</span><span class="sub" style="margin-left:auto;color:var(--muted);font-size:12px">${esc(f.id)}</span></div>
        <div class="claim">«${esc(f.claim)}»</div>${f.kernel ? `<div class="ex"><b>Зерно правди:</b> ${esc(f.kernel)}</div>` : ""}<div class="ex">${linkify(f.explanation)}</div>
        ${f.spread ? `<div class="ex" style="font-size:13px"><b>Поширення:</b> ${esc(f.spread)}</div>` : ""}${(f.sources || []).length ? `<div class="src">${f.sources.map((s) => `<span>${linkify(s)}</span>`).join("")}</div>` : ""}</div>`).join("")}</div>`;
    body().querySelectorAll(".filters button").forEach((b) => (b.onclick = () => { filter = b.dataset.v || null; draw(); }));
  };
  draw();
}

// ---------------------------------------------------------------- judgments
async function renderJudgments() {
  body().innerHTML = skeleton(3);
  const js = await api(`/api/cases/${encodeURIComponent(S.slug)}/records/judgments`);
  if (!js.length) { body().innerHTML = `<div class="empty"><b>Власних висновків ще немає</b>Аналітик має займати позицію: твердження + ймовірність + на чому вона тримається.</div>`; return; }
  body().innerHTML = `<div class="sub" style="color:var(--muted);margin-bottom:12px">Оцінки аналітика, а не документи. Ймовірність — наскільки правдоподібно; рівень — на чому це тримається.</div>
    <div class="grid">${[...js].sort((a, b) => b.probability - a.probability).map((j) => `<div class="card judg"><div><div class="s">${esc(j.statement)}</div>
      <div style="display:flex;gap:8px;margin-top:6px;align-items:center"><span class="lvl">${esc(j.level)} · ${esc(LEVEL_TXT[j.level] || "")}</span><span class="sub" style="color:var(--muted);font-size:12px">${esc(j.id)} · ${fmtDate(j.recorded_at)}</span></div>
      <details><summary>Аргументи й альтернативи</summary><p>${linkify(j.reasoning)}</p>${j.alternatives ? `<p><b>Альтернативи:</b> ${esc(j.alternatives)}</p>` : ""}${j.would_change_my_mind ? `<p><b>Що змінить оцінку:</b> ${esc(j.would_change_my_mind)}</p>` : ""}</details></div>
      <div><div class="pval">${pct(j.probability)}</div><div class="pbar" role="img" aria-label="${pct(j.probability)}"><i style="width:${j.probability * 100}%"></i></div></div></div>`).join("")}</div>`;
}

// ---------------------------------------------------------------- scenarios
async function renderScenarios() {
  body().innerHTML = skeleton(3);
  const st = await api(`/api/cases/${encodeURIComponent(S.slug)}/forecast`);
  if (!st) { body().innerHTML = `<div class="empty"><b>Сценаріїв ще немає</b>Попросіть аналітика сформулювати 2–4 взаємовиключні сценарії з індикаторами і датами перевірки.</div>`; return; }
  const ids = Object.keys(st.scenarios), col = Object.fromEntries(ids.map((k, i) => [k, SCOL[i] || "var(--muted)"]));
  const sc = st.model.scenarios;
  const bar = (p, sm) => `<div class="stack ${sm ? "sm" : ""}">${ids.map((k) => `<div style="flex:${Math.max(p[k], 0.0001)};background:${col[k]}" title="${esc(st.scenario_names[k])}: ${pct(p[k])}">${p[k] > (sm ? 0.12 : 0.08) ? pct(p[k]) : ""}</div>`).join("")}</div>`;
  const legend = `<div class="legend">${ids.map((k) => `<span><i style="background:${col[k]}"></i>${esc(k)}. ${esc(st.scenario_names[k])}</span>`).join("")}</div>`;
  const hist = st.history || [];
  body().innerHTML = `<div class="card"><div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px"><h3 style="margin:0">Куди рухається ситуація${st.model.horizon ? ` · до ${fmtDate(st.model.horizon)}` : ""}</h3><span class="chip">станом на ${fmtDate(st.as_of)}</span></div>${bar(st.scenarios)}<div style="margin-top:12px">${legend}</div></div>
    <div class="grid g${Math.min(ids.length, 3)}" style="margin-top:16px">${sc.map((s) => `<div class="card scen-card"><div class="sub"><span class="sw" style="background:${col[s.id]}"></span>Сценарій ${esc(s.id)}</div><div class="p">${pct(st.scenarios[s.id])}</div><h3>${esc(s.name)}</h3><div class="sub">${esc(s.summary || "")}</div>${s.resolve ? `<details style="margin-top:8px;font-size:13px;color:var(--text-2)"><summary style="cursor:pointer;color:var(--muted)">Як перевіримо</summary>${esc(s.resolve)}</details>` : ""}</div>`).join("")}</div>
    ${hist.length > 1 ? `<div class="section-title"><h2>Як змінювалась оцінка</h2></div><div class="card">${historyChart(hist, ids, col)}</div>` : ""}
    ${st.whatif.length ? `<div class="section-title"><h2>Що зсуне оцінку</h2><span class="sub" style="color:var(--muted);font-size:13px">якщо подія станеться / якщо її вікно закриється без неї</span></div>
    <div class="card"><div class="whatif">${st.whatif.map((w) => `<div class="nm">${esc(w.id)}. ${esc(w.name)}<div class="sub" style="color:var(--muted);font-size:12px">до ${fmtDate(w.window_end)} · ймовірність події ${pct(w.implied)}</div></div><div class="k">так<br>ні</div><div class="bars" style="display:flex;flex-direction:column;gap:5px">${bar(w.yes, true)}${bar(w.no, true)}</div><hr>`).join("")}</div></div>` : ""}
    ${st.coherence.some((c) => c.flag) ? `<div class="hint" style="margin-top:16px">Неузгодженість: ${st.coherence.filter((c) => c.flag).map((c) => `${esc(c.indicator)} ↔ ${esc(c.forecast)} (${c.diff_pp.toFixed(0)} п. п.)`).join(", ")}</div>` : ""}
    ${st.forecasts.length ? `<div class="section-title"><h2>Окремі прогнози</h2><span class="chip">перевірено ${st.track_record.resolved} з ${st.forecasts.length}${st.track_record.mean_brier != null ? ` · Браєр ${st.track_record.mean_brier.toFixed(2)}` : ""}</span></div>
    <div class="card tbl-wrap"><table class="tbl"><thead><tr><th></th><th>Прогноз</th><th>Ймовірність</th><th>Перевірка</th><th>Стан</th></tr></thead><tbody>${[...st.forecasts].sort((a, b) => a.resolve_by.localeCompare(b.resolve_by)).map((f) => `<tr><td class="mono">${esc(f.id)}</td><td>${esc(f.text)}</td><td style="min-width:150px"><div style="display:flex;gap:10px;align-items:center"><b style="width:44px">${pct(f.p_now)}</b><div class="pbar" style="flex:1"><i style="width:${f.p_now * 100}%"></i></div></div></td><td style="white-space:nowrap">${fmtDate(f.resolve_by)}</td><td>${f.status === "resolved" ? `<span class="pill fin">${f.outcome ? "так" : "ні"} · ${f.brier.toFixed(2)}</span>` : f.status === "overdue" ? `<span class="pill bad">перевірити</span>` : `<span class="pill run">чекаємо</span>`}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
}
function historyChart(hist, ids, col) {
  const W = 900, H = 220, P = 30, n = hist.length;
  const x = (i) => P + (i * (W - 2 * P)) / Math.max(n - 1, 1), y = (v) => H - 24 - v * (H - 44);
  const lines = ids.map((k) => `<polyline fill="none" stroke="${col[k]}" stroke-width="2.4" stroke-linejoin="round" points="${hist.map((h, i) => `${x(i)},${y(h[k])}`).join(" ")}"/>` +
    hist.map((h, i) => `<circle cx="${x(i)}" cy="${y(h[k])}" r="4" fill="${col[k]}" stroke="var(--panel-solid)" stroke-width="2"><title>${esc(h.date)} · ${esc(k)}: ${pct(h[k])}\n${esc(h.event)}</title></circle>`).join("")).join("");
  const grid = [0, 0.25, 0.5, 0.75, 1].map((v) => `<line x1="${P}" x2="${W - P}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)"/><text x="${P - 6}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${v * 100}</text>`).join("");
  const xl = hist.map((h, i) => `<text x="${x(i)}" y="${H - 6}" text-anchor="middle" font-size="11" fill="var(--muted)">${esc(h.date.slice(8, 10) + "." + h.date.slice(5, 7))}</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Історія ймовірностей сценаріїв">${grid}${lines}${xl}</svg>`;
}

// ---------------------------------------------------------------- data, report, log
async function renderData() {
  body().innerHTML = skeleton(2);
  const [files, jobs] = await Promise.all([api(`/api/cases/${encodeURIComponent(S.slug)}/files?dir=data`), api(`/api/cases/${encodeURIComponent(S.slug)}/jobs`)]);
  const size = (b) => b > 1e6 ? (b / 1e6).toFixed(1) + " МБ" : b > 1e3 ? (b / 1e3).toFixed(0) + " КБ" : b + " Б";
  body().innerHTML = `<div class="section-title" style="margin-top:0"><h2>Фонові збори</h2></div>
    ${jobs.length ? `<div class="grid g2">${jobs.map((j) => `<div class="card"><div style="display:flex;justify-content:space-between;gap:10px"><b class="mono">${esc(j.id)}</b>${jobPill(j)}</div><div class="sub" style="color:var(--muted);font-size:12px;margin-top:4px">почато ${fmtDate(j.started)} · ${ago(j.started)}</div><pre class="logtail">${esc((j.log_tail || []).join("\n"))}</pre></div>`).join("")}</div>` : `<div class="empty"><b>Зборів ще не було</b>Аналітик запускає їх інструментами collect_telegram, collect_youtube, collect_news.</div>`}
    <div class="section-title"><h2>Файли даних</h2><span class="chip">${files.length}</span></div>
    ${files.length ? `<div class="card tbl-wrap"><table class="tbl"><thead><tr><th>Файл</th><th>Розмір</th><th>Змінено</th></tr></thead><tbody>${files.map((f) => `<tr><td class="mono"><a href="${fileUrl(S.slug, f.path)}" target="_blank" rel="noopener">${esc(f.path)}</a></td><td style="white-space:nowrap">${size(f.size)}</td><td style="white-space:nowrap">${ago(f.mtime)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">Порожньо</div>`}`;
}
async function renderReport() {
  body().innerHTML = skeleton(1);
  const files = await api(`/api/cases/${encodeURIComponent(S.slug)}/files?dir=report`);
  const html = files.find((f) => /report\.html$/.test(f.path)) || files.find((f) => f.path.endsWith(".html"));
  const pdfs = files.filter((f) => f.path.endsWith(".pdf"));
  body().innerHTML = `<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px"><button class="btn primary" id="build2"><span class="i-doc"></span>Зібрати звіт</button>
      ${pdfs.map((p) => `<a class="btn" href="${fileUrl(S.slug, p.path)}" target="_blank" rel="noopener">PDF · ${esc(p.path.split("/").pop())}</a>`).join("")}
      ${html ? `<a class="btn" href="${fileUrl(S.slug, html.path)}" target="_blank" rel="noopener">Відкрити окремо</a><span class="sub" style="color:var(--muted);font-size:13px">оновлено ${ago(html.mtime)}</span>` : ""}</div>
    ${html ? `<iframe class="report-frame" src="${fileUrl(S.slug, html.path)}" title="Звіт"></iframe>` : `<div class="empty"><b>Звіт ще не зібрано</b>Аналітик пише report/report.md (або .tex); кнопка вище збирає HTML, а за наявності pandoc чи tectonic — і PDF.</div>`}`;
  $("#build2").onclick = buildReport;
}
async function buildReport() {
  toast("Збираю звіт…");
  try { const r = await api(`/api/cases/${encodeURIComponent(S.slug)}/report`, { method: "POST" }); toast(r.pdf ? "Готово: HTML і PDF" : "Готово: HTML"); if (S.tab === "report") renderReport(); else go(`#/c/${encodeURIComponent(S.slug)}/report`); }
  catch (e) { toast(e.message, 6000); }
}
async function renderLog() {
  const r = await api(`/api/cases/${encodeURIComponent(S.slug)}/log`);
  body().innerHTML = `<div class="card loglines prose">${r.html || "<p>Порожньо</p>"}</div>`;
}

// ---------------------------------------------------------------- boot
(async function boot() {
  try {
    S.meta = await api("/api/meta");
    $("#version").textContent = `версія ${S.meta.version}`;
    await loadCases();
    route();
  } catch (e) {
    view().innerHTML = `<div class="empty" style="margin-top:60px"><b>Не вдалося підключитися</b>${esc(e.message)}</div>`;
  }
})();

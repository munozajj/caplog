"use strict";

const $ = (id) => document.getElementById(id);
let maxChars = 500;
let lastSeq = -1;
let state = {};
let csrf = "";
let display = {};
let corrects = null;
const records = new Map();

const norm = (s) => s.replace(/\r\n?/g, "\n");
const chars = (s) => [...norm(s)].length;

function headers() {
  return { "Content-Type": "application/json", "X-CapLog": "1", "X-CSRF": csrf };
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: headers(),
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.status + " " + res.statusText;
    try { msg = (await res.json()).error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res;
}

function confirmDialog(text, preview) {
  return new Promise((resolve) => {
    const backdrop = $("backdrop");
    $("confirm-text").textContent = text;
    const box = $("confirm-preview");
    box.textContent = preview || "";
    box.hidden = !preview;
    const finish = (ok) => {
      backdrop.hidden = true;
      $("confirm-ok").onclick = null;
      $("confirm-cancel").onclick = null;
      document.removeEventListener("keydown", onKey);
      resolve(ok);
    };
    const onKey = (e) => { if (e.key === "Escape") finish(false); };
    $("confirm-ok").onclick = () => finish(true);
    $("confirm-cancel").onclick = () => finish(false);
    document.addEventListener("keydown", onKey);
    backdrop.hidden = false;
    $("confirm-cancel").focus();
  });
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function metaOf(record) {
  try { return record.meta ? JSON.parse(record.meta) : {}; } catch (_) { return {}; }
}

function formatWhen(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  if (display.time_zone === "utc") {
    const hh = String(d.getUTCHours()).padStart(2, "0");
    const mm = String(d.getUTCMinutes()).padStart(2, "0");
    const hour = display.clock === "12" ? ((d.getUTCHours() % 12) || 12) + (d.getUTCHours() < 12 ? " AM" : " PM") : hh + ":" + mm;
    return display.clock === "12"
      ? d.getUTCFullYear() + "-" + (d.getUTCMonth() + 1) + "-" + d.getUTCDate() + " " + hour
      : d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }
  return display.clock === "12" ? d.toLocaleString([], { hour12: true }) : d.toLocaleString([], { hour12: false });
}

function posterLabel(record, meta) {
  if (meta.poster === "account") return "Account " + (meta.username || "");
  if (meta.poster === "guest") return "Guest";
  if (record.kind === "genesis") return "Title";
  if (record.kind === "system") return "System";
  if (record.kind === "export" || record.kind === "import") return record.kind;
  return "";
}

function applyPosition() {
  const log = $("log");
  log.className = "layout pos-" + (display.post_box || "top");
}

function renderAll() {
  const box = $("entries");
  box.textContent = "";
  const rows = [...records.values()].sort((a, b) => b.seq - a.seq);
  rows.forEach((record) => box.append(cardFor(record)));
}

function cardFor(record) {
  const meta = metaOf(record);
  const card = el("article", "entry");
  card.id = "e" + record.seq;
  if (record.kind !== "entry") card.classList.add("system");
  const firstLine = (record.body || "").split("\n")[0];
  const toggle = el("button", "toggle");
  toggle.type = "button";
  const top = el("div", "top");
  if (display.show_number !== "false") top.append(el("span", "num", "#" + String(record.seq).padStart(4, "0")));
  if (display.show_time !== "false") {
    const when = el("time", "when", formatWhen(record.created_at));
    when.dateTime = record.created_at;
    when.title = record.created_at + " UTC";
    top.append(when);
  }
  toggle.append(top);
  const who = el("p", "who");
  if (display.show_name !== "false") who.append(el("span", "name", (record.first_name + " " + record.last_name).trim() || "System"));
  if (display.show_poster !== "false") who.append(el("span", "marker", posterLabel(record, meta)));
  if (display.show_tags !== "false" && meta.tag) who.append(el("span", "marker", meta.tag));
  toggle.append(who);
  toggle.append(el("p", "line", firstLine));
  const full = el("div", "full");
  full.hidden = true;
  full.append(el("p", "body", record.body || ""));
  if (display.show_ip !== "false" && state.settings.show_ips !== "false" && record.ip) {
    full.append(el("div", "ip", record.ip));
  }
  full.append(el("div", "meta", "Type: " + record.kind));
  full.append(el("div", "meta hash", record.hash));
  if (meta.corrects) full.append(el("div", "meta", "Corrects entry #" + meta.corrects));
  const links = (state.corrections && state.corrections[record.seq]) || [];
  links.forEach((seq) => full.append(el("div", "meta", "Corrected by entry #" + seq)));
  if (state.settings.feature_corrections === "true" && record.kind === "entry") {
    const button = el("button", "btn", "Correct entry #" + record.seq);
    button.type = "button";
    button.onclick = (event) => {
      event.stopPropagation();
      corrects = record.seq;
      $("correcting").hidden = false;
      $("correcting").textContent = "This new entry will be linked to #" + record.seq + ". The original entry is not changed.";
      $("text").focus();
    };
    full.append(button);
  }
  toggle.onclick = () => { full.hidden = !full.hidden; card.classList.toggle("open", !full.hidden); };
  card.append(toggle, full);
  return card;
}

function upsert(record) {
  records.set(record.seq, record);
  if (record.seq > lastSeq) lastSeq = record.seq;
  renderAll();
}

async function loadAll() {
  const params = new URLSearchParams();
  params.set("limit", "1000");
  const q = $("q").value.trim();
  const name = $("name-filter").value.trim();
  const tag = $("tag-filter").value.trim();
  const start = $("start").value.trim();
  const end = $("end").value.trim();
  if (q) params.set("q", q);
  if (name) params.set("name", name);
  if (tag) params.set("tag", tag);
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  records.clear();
  lastSeq = -1;
  const res = await fetch("api/records?" + params.toString());
  const rows = await res.json();
  if (!Array.isArray(rows)) throw new Error(rows.error || "could not load entries");
  rows.forEach((row) => { records.set(row.seq, row); if (row.seq > lastSeq) lastSeq = row.seq; });
  renderAll();
  const jump = $("jump").value.trim();
  if (jump) {
    const node = $("e" + jump);
    if (node) node.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function connectLive() {
  const es = new EventSource("api/events?after=" + lastSeq);
  const live = $("live");
  es.onopen = () => live.classList.remove("off");
  es.onerror = () => live.classList.add("off");
  es.addEventListener("record", (e) => upsert(JSON.parse(e.data)));
}

function setupEntryForm() {
  const text = $("text");
  const count = $("count");
  const msg = $("form-msg");
  const update = () => {
    const n = chars(text.value);
    count.textContent = n + " / " + maxChars;
    count.classList.toggle("err", n > maxChars);
  };
  text.addEventListener("input", update);
  $("entry-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    msg.textContent = "";
    const body = norm(text.value).trim();
    const account = state.user && state.settings.post_mode !== "guests";
    const first = account ? state.user.first_name : $("first").value.trim();
    const last = account ? state.user.last_name : $("last").value.trim();
    if (!account && (!first || !last)) { msg.textContent = "First and last name are required."; return; }
    if (!body) { msg.textContent = "Entry text is required."; return; }
    if (chars(body) > maxChars) { msg.textContent = "Entry is longer than " + maxChars + " characters."; return; }
    const ok = await confirmDialog("This entry is permanent and can never be edited or deleted.", body);
    if (!ok) return;
    const payload = { text: body };
    if (!account) { payload.first_name = first; payload.last_name = last; }
    if ($("tag").value) payload.tag = $("tag").value;
    if (corrects) payload.corrects = corrects;
    const btn = $("add-btn");
    btn.disabled = true;
    try {
      const res = await post("api/entries", payload);
      upsert(await res.json());
      text.value = "";
      corrects = null;
      $("correcting").hidden = true;
      update();
    } catch (err) {
      msg.textContent = err.message;
    } finally {
      btn.disabled = false;
      text.focus();
    }
  });
  $("clear-correct").onclick = () => { corrects = null; $("correcting").hidden = true; };
}

function readDisplay() {
  const next = {};
  document.querySelectorAll("[data-display]").forEach((node) => {
    const key = node.dataset.display;
    next[key] = node.type === "checkbox" ? (node.checked ? "true" : "false") : node.value;
  });
  return next;
}

function writeDisplay(values) {
  display = Object.assign({
    show_number: "true", show_name: "true", show_time: "true", show_ip: "true",
    show_poster: "true", show_tags: "true", time_zone: "local", clock: "24", post_box: "top",
  }, values);
  document.querySelectorAll("[data-display]").forEach((node) => {
    const key = node.dataset.display;
    if (display[key] === undefined) return;
    if (node.type === "checkbox") node.checked = display[key] !== "false";
    else node.value = display[key];
  });
  applyPosition();
  if ($("entries").childElementCount) renderAll();
}

async function saveDisplay() {
  display = readDisplay();
  applyPosition();
  renderAll();
  if (state.settings.users_may_change_display !== "true") return;
  if (state.user) {
    try { await post("api/display", display); } catch (err) { $("form-msg").textContent = err.message; }
  } else {
    localStorage.setItem("caplog-display", JSON.stringify(display));
  }
}

async function doExport(format) {
  const ok = await confirmDialog("Exporting adds a permanent system entry to the log.", "");
  if (!ok) return;
  const res = await post("api/export/" + format, "{}");
  const blob = await res.blob();
  const match = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "");
  const link = el("a");
  link.href = URL.createObjectURL(blob);
  link.download = match ? match[1] : "caplog." + format;
  document.body.append(link);
  link.click();
  setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
}

async function doImport(file) {
  const text = await file.text();
  let info;
  try { info = JSON.parse(text); } catch (_) { alert("That file is not valid JSON."); return; }
  const n = Array.isArray(info.records) ? Math.max(0, info.records.length - 1) : 0;
  const ok = await confirmDialog(
    "Import " + n + " record(s) from " + (info.title || "this file") + ". The chain is checked first. This cannot be undone.",
    ""
  );
  if (!ok) return;
  const res = await post("api/import", text);
  const body = await res.json();
  alert("Imported " + body.imported + " record(s) as entries #" + body.first_seq + "-#" + body.last_seq + ".");
  await loadAll();
}

function applySettings() {
  const s = state.settings;
  document.documentElement.style.setProperty("--accent", s.accent_color || "#0d6e6a");
  $("notice").hidden = !(s.feature_notice === "true" && s.notice);
  $("notice").textContent = s.notice || "";
  $("clock-banner").hidden = !state.clock_warning;
  $("clock-banner").textContent = state.clock_warning || "";
  $("search-box").hidden = s.feature_search !== "true";
  $("display-link").hidden = s.feature_display_page !== "true";
  const tags = (s.tags || "").split(",").filter(Boolean);
  $("tag-wrap").hidden = s.feature_tags !== "true" || tags.length === 0;
  const select = $("tag");
  select.textContent = "";
  select.append(new Option("(no tag)", ""));
  tags.forEach((tag) => select.append(new Option(tag, tag)));
  const may = s.users_may_change_display === "true";
  $("display-box").hidden = !may;
  const accountPost = state.user && s.post_mode !== "guests";
  $("name-fields").hidden = !!accountPost || s.post_mode === "accounts" && !state.user;
  if (s.post_mode === "accounts" && !state.user) $("name-fields").hidden = true;
  $("account-line").hidden = !accountPost;
  if (accountPost) $("account-line").textContent = "Posting as " + state.user.first_name + " " + state.user.last_name + " (Account " + state.user.username + ")";
  $("sign-btn").textContent = state.user ? "Sign out" : "Sign in";
  if (s.post_mode === "accounts" && !state.user) $("form-msg").textContent = "Sign in to add an entry.";
}

async function openHelp() {
  const panel = $("help");
  if (!panel.hidden) { panel.hidden = true; return; }
  const res = await fetch("/static/help/user.html");
  panel.innerHTML = await res.text();
  panel.hidden = false;
}

async function start() {
  state = await (await fetch("api/state")).json();
  csrf = state.csrf;
  maxChars = state.max_text_chars || 500;
  if (state.setup_needed || state.admin_needed) {
    $("setup").hidden = false;
    $("title-wrap").hidden = !state.setup_needed;
    $("setup-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const title = $("title").value.trim();
      if (state.setup_needed && !title) return;
      const ok = await confirmDialog("The title is permanent and can never be changed. The admin account cannot edit or delete entries.", title);
      if (!ok) return;
      try {
        await post("api/setup", {
          title, username: $("setup-user").value.trim(),
          first_name: $("setup-first").value.trim(), last_name: $("setup-last").value.trim(),
          password: $("setup-pass").value,
        });
        location.reload();
      } catch (err) { alert(err.message); }
    });
    return;
  }
  const defaults = {};
  ["show_number","show_name","show_time","show_ip","show_poster","show_tags","time_zone","clock","post_box"].forEach((key) => {
    defaults[key] = state.settings[key];
  });
  let chosen = {};
  if (state.settings.users_may_change_display === "true") {
    if (state.user) chosen = state.user_display || {};
    else {
      try { chosen = JSON.parse(localStorage.getItem("caplog-display") || "{}"); } catch (_) { chosen = {}; }
    }
  }
  writeDisplay(Object.assign({}, defaults, chosen));
  $("actions").hidden = false;
  $("log").hidden = false;
  $("log-title").textContent = state.title;
  document.title = state.title + " - CapLog";
  applySettings();
  setupEntryForm();
  document.querySelectorAll("[data-display]").forEach((node) => node.addEventListener("change", () => saveDisplay()));
  $("expand-all").onclick = () => document.querySelectorAll(".entry .full").forEach((node) => { node.hidden = false; });
  $("collapse-all").onclick = () => document.querySelectorAll(".entry .full").forEach((node) => { node.hidden = true; });
  $("search-btn").onclick = () => loadAll().catch((err) => alert(err.message));
  $("export-json").onclick = () => doExport("json").catch((err) => alert(err.message));
  $("export-pdf").onclick = () => doExport("pdf").catch((err) => alert(err.message));
  $("import-btn").onclick = () => $("import-file").click();
  $("import-file").onchange = (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) doImport(file).catch((err) => alert(err.message));
  };
  $("help-btn").onclick = () => openHelp().catch((err) => alert(err.message));
  $("sign-btn").onclick = async () => {
    if (state.user) {
      await post("api/logout", {});
      location.reload();
      return;
    }
    $("signin").hidden = false;
  };
  $("signin-cancel").onclick = () => { $("signin").hidden = true; };
  $("signin-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await post("api/login", { username: $("login-user").value.trim(), password: $("login-pass").value });
      location.reload();
    } catch (err) { $("login-msg").textContent = err.message; }
  });
  await loadAll();
  connectLive();
}

start().catch((err) => { document.body.textContent = "CapLog failed to load: " + err.message; });

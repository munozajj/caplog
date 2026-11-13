// CapLog front end. Plain JavaScript, no framework, no build step.
"use strict";

const $ = (id) => document.getElementById(id);
const MAX = 500; // must match the server's limit
let lastSeq = -1; // newest entry number shown on screen

// Count characters the same way the server does: Unicode characters, with
// Windows line endings turned into single newlines.
const norm = (s) => s.replace(/\r\n?/g, "\n");
const chars = (s) => [...norm(s)].length;

// POST helper. The X-CapLog header is required by the server for writes.
async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CapLog": "1" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.status + " " + res.statusText;
    try { msg = (await res.json()).error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res;
}

// Show the confirmation dialog; resolves true only if the user confirms.
function confirmDialog(title, text, preview, okLabel) {
  return new Promise((resolve) => {
    const d = $("confirm");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    $("confirm-preview").textContent = preview || "";
    $("confirm-preview").hidden = !preview;
    $("confirm-ok").textContent = okLabel || "Save permanently";
    d.returnValue = "cancel";
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.showModal();
    d.querySelector('button[value="cancel"]').focus(); // safe default
  });
}

// ---- rendering ----

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text; // textContent = never interpreted as HTML
  return e;
}

function localTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

function render(r) {
  if (r.seq <= lastSeq) return; // already shown
  lastSeq = r.seq;
  const box = $("entries");
  const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 200;

  const div = el("article", "entry");
  div.id = "e" + r.seq;
  const head = el("div", "head");
  head.append(el("span", "num", "#" + String(r.seq).padStart(4, "0")), " ");
  const when = el("span", "when", localTime(r.created_at));
  when.title = r.created_at + " (UTC)";
  head.append(when, " ");
  let meta = null;
  try { meta = r.meta ? JSON.parse(r.meta) : null; } catch (_) {}

  switch (r.kind) {
    case "genesis":
      div.classList.add("system");
      head.append("LOGBOOK OPENED");
      div.append(head, el("p", "body", "Title: " + r.body));
      break;
    case "entry":
      head.append(r.last_name + ", " + r.first_name);
      if (r.ip) head.append(" ", el("span", "ip", r.ip));
      div.append(head, el("p", "body", r.body));
      break;
    case "imported":
      head.append(r.first_name || r.last_name ? r.last_name + ", " + r.first_name : "SYSTEM (" + (meta && meta.source_kind) + ")");
      if (r.ip) head.append(" ", el("span", "ip", r.ip));
      head.append(el("span", "tag", "IMPORTED"));
      div.append(head);
      if (meta) div.append(el("div", "meta", "from \u201c" + meta.source_title + "\u201d #" + meta.source_seq + ", " + localTime(meta.source_created_at)));
      div.append(el("p", "body", r.body));
      break;
    default: // export / import system notes
      div.classList.add("system");
      head.append("SYSTEM: " + r.kind.toUpperCase());
      div.append(head, el("p", "body", r.body));
  }
  box.append(div);
  if (nearBottom) div.scrollIntoView({ block: "end" });
}

// ---- loading + live updates ----

async function loadAll() {
  for (;;) {
    const res = await fetch("api/records?limit=1000&after=" + lastSeq);
    const recs = await res.json();
    recs.forEach(render);
    if (recs.length < 1000) break;
  }
}

function connectLive() {
  // EventSource reconnects by itself and sends Last-Event-ID, so the server
  // fills in anything missed while disconnected.
  const es = new EventSource("api/events?after=" + lastSeq);
  const live = $("live");
  es.onopen = () => { live.textContent = "\u25CF live"; live.classList.add("on"); };
  es.onerror = () => { live.textContent = "\u25CF reconnecting"; live.classList.remove("on"); };
  es.addEventListener("record", (e) => render(JSON.parse(e.data)));
}

// ---- entry form ----

function setupEntryForm() {
  const text = $("text"), count = $("count"), msg = $("form-msg");
  const update = () => {
    const n = chars(text.value);
    count.textContent = n + " / " + MAX;
    count.classList.toggle("err", n > MAX);
  };
  text.addEventListener("input", update);
  // Enter submits, Shift+Enter makes a new line.
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("entry-form").requestSubmit();
    }
  });

  $("entry-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    msg.textContent = "";
    const first = $("first").value.trim(), last = $("last").value.trim();
    const body = norm(text.value).trim();
    if (!first || !last) { msg.textContent = "First and last name are required."; return; }
    if (!body) { msg.textContent = "Entry text is required."; return; }
    if (chars(body) > MAX) { msg.textContent = "Entry is longer than " + MAX + " characters."; return; }

    // Step 2: explicit confirmation that this is permanent.
    const ok = await confirmDialog(
      "Confirm permanent entry",
      "Every entry is PERMANENT. Once saved it can never be deleted or modified by anyone. Your IP address will be recorded on the entry. Save this entry as " + first + " " + last + "?",
      body);
    if (!ok) return;

    const btn = e.submitter || $("entry-form").querySelector("button");
    btn.disabled = true;
    try {
      const res = await post("api/entries", { first_name: first, last_name: last, text: body });
      render(await res.json());
      text.value = "";
      update();
    } catch (err) {
      msg.textContent = err.message;
    } finally {
      btn.disabled = false;
      text.focus();
    }
  });
}

// ---- export / import ----

async function doExport(format) {
  const ok = await confirmDialog("Export " + format.toUpperCase(),
    "Exporting adds a permanent system entry to the log recording that an export happened.", "", "Export");
  if (!ok) return;
  try {
    const res = await post("api/export/" + format, "{}");
    const blob = await res.blob();
    const m = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "");
    const a = el("a");
    a.href = URL.createObjectURL(blob);
    a.download = m ? m[1] : "caplog." + format;
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  } catch (err) {
    alert("Export failed: " + err.message);
  }
}

async function doImport(file) {
  const textData = await file.text();
  let info;
  try { info = JSON.parse(textData); } catch (_) { alert("That file is not valid JSON."); return; }
  const n = Array.isArray(info.records) ? info.records.length - 1 : 0;
  const ok = await confirmDialog("Import entries",
    "This will permanently append " + n + " record(s) from \u201c" + info.title + "\u201d to the end of this log, " +
    "marked as imported, plus system entries recording the import. The file's hash chain is checked first. This cannot be undone.",
    "", "Import permanently");
  if (!ok) return;
  try {
    const res = await post("api/import", textData);
    const r = await res.json();
    alert("Imported " + r.imported + " record(s) as entries #" + r.first_seq + "-#" + r.last_seq + ".");
  } catch (err) {
    alert("Import rejected: " + err.message);
  }
}

// ---- start ----

async function start() {
  const state = await (await fetch("api/state")).json();
  if (state.setup_needed) {
    $("setup").hidden = false;
    $("setup-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const title = $("title").value.trim();
      if (!title) return;
      const ok = await confirmDialog("Set the logbook title",
        "The title is PERMANENT and can never be changed. Use this title?", title, "Set title permanently");
      if (!ok) return;
      try { await post("api/setup", { title }); location.reload(); }
      catch (err) { alert(err.message); location.reload(); }
    });
    return;
  }
  $("log").hidden = false;
  $("log-title").textContent = state.title;
  document.title = state.title + " - CapLog";
  setupEntryForm();
  $("export-json").onclick = () => doExport("json");
  $("export-pdf").onclick = () => doExport("pdf");
  $("import-btn").onclick = () => $("import-file").click();
  $("import-file").onchange = (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) doImport(f); };
  await loadAll();
  window.scrollTo(0, document.body.scrollHeight);
  connectLive();
}

start().catch((err) => { document.body.textContent = "CapLog failed to load: " + err.message; });

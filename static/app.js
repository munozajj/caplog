"use strict";

const $ = (id) => document.getElementById(id);
let maxChars = 500;
let lastSeq = -1;

const norm = (s) => s.replace(/\r\n?/g, "\n");
const chars = (s) => [...norm(s)].length;

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

function localTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

function render(record) {
  if (record.seq <= lastSeq) return;
  lastSeq = record.seq;
  const box = $("entries");
  const card = el("article", "entry");
  card.id = "e" + record.seq;
  const top = el("div", "top");
  top.append(el("span", "num", "#" + String(record.seq).padStart(4, "0")));
  let meta = null;
  try { meta = record.meta ? JSON.parse(record.meta) : null; } catch (_) {}
  const system = record.kind === "genesis" || record.kind === "export" || record.kind === "import";
  if (system) card.classList.add("system");
  if (record.kind === "genesis") {
    top.append(el("span", "name", "Title set"));
  } else if (record.kind === "entry" || (record.kind === "imported" && (record.first_name || record.last_name))) {
    top.append(el("span", "name", (record.first_name + " " + record.last_name).trim()));
  } else if (record.kind === "imported") {
    card.classList.add("system");
    top.append(el("span", "name", "System (" + ((meta && meta.source_kind) || "imported") + ")"));
  } else {
    top.append(el("span", "name", record.kind === "export" ? "Export" : "Import"));
  }
  const when = el("time", "when", localTime(record.created_at));
  when.dateTime = record.created_at;
  when.title = record.created_at + " (UTC)";
  top.append(when);
  card.append(top);
  if (!system && record.ip) card.append(el("div", "ip", record.ip));
  if (record.kind === "imported" && meta) {
    card.append(el("div", "meta", "Imported from " + meta.source_title + " #" + meta.source_seq));
  }
  const body = record.kind === "genesis" ? record.body : record.body;
  card.append(el("p", "body", body));
  box.prepend(card);
}

async function loadAll() {
  for (;;) {
    const res = await fetch("api/records?limit=1000&after=" + lastSeq);
    const rows = await res.json();
    rows.forEach(render);
    if (rows.length < 1000) break;
  }
}

function connectLive() {
  const es = new EventSource("api/events?after=" + lastSeq);
  const live = $("live");
  es.onopen = () => live.classList.remove("off");
  es.onerror = () => live.classList.add("off");
  es.addEventListener("record", (e) => render(JSON.parse(e.data)));
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
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("entry-form").requestSubmit();
    }
  });
  $("entry-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    msg.textContent = "";
    const first = $("first").value.trim();
    const last = $("last").value.trim();
    const body = norm(text.value).trim();
    if (!first || !last) { msg.textContent = "First and last name are required."; return; }
    if (!body) { msg.textContent = "Entry text is required."; return; }
    if (chars(body) > maxChars) { msg.textContent = "Entry is longer than " + maxChars + " characters."; return; }
    const ok = await confirmDialog("This entry is permanent and can never be edited or deleted.", body);
    if (!ok) return;
    const btn = $("add-btn");
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
}

async function start() {
  const state = await (await fetch("api/state")).json();
  maxChars = state.max_text_chars || 500;
  if (state.setup_needed) {
    $("setup").hidden = false;
    $("setup-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const title = $("title").value.trim();
      if (!title) return;
      const ok = await confirmDialog("The title is permanent and can never be changed.", title);
      if (!ok) return;
      try {
        await post("api/setup", { title });
        location.reload();
      } catch (err) {
        alert(err.message);
      }
    });
    return;
  }
  $("actions").hidden = false;
  $("log").hidden = false;
  $("log-title").textContent = state.title;
  document.title = state.title + " - CapLog";
  setupEntryForm();
  $("export-json").onclick = () => doExport("json").catch((err) => alert(err.message));
  $("export-pdf").onclick = () => doExport("pdf").catch((err) => alert(err.message));
  $("import-btn").onclick = () => $("import-file").click();
  $("import-file").onchange = (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) doImport(file).catch((err) => alert(err.message));
  };
  await loadAll();
  connectLive();
}

start().catch((err) => { document.body.textContent = "CapLog failed to load: " + err.message; });

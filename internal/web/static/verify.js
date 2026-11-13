// CapLog verify page: asks the server to recheck the whole chain.
"use strict";

const $ = (id) => document.getElementById(id);

function add(parent, tag, text, cls) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  e.textContent = text;
  parent.append(e);
  return e;
}

async function run() {
  const result = $("result"), details = $("details");
  result.textContent = "Checking\u2026";
  result.className = "";
  details.replaceChildren();
  const rep = await (await fetch("api/verify")).json();

  result.textContent = rep.ok ? "\u2714 " + rep.summary : "\u2718 " + rep.summary;
  result.className = rep.ok ? "ok" : "bad";

  if (rep.problems.length) {
    add(details, "h3", "Problems (possible tampering)", "bad");
    const ul = add(details, "ul", "");
    rep.problems.forEach((p) => add(ul, "li", p, "bad"));
  }
  if (rep.warnings.length) {
    add(details, "h3", "Warnings", "warn");
    const ul = add(details, "ul", "");
    rep.warnings.forEach((w) => add(ul, "li", w, "warn"));
  }
  add(details, "h3", "Details");
  const kv = add(details, "div", "", "kv");
  const row = (k, v) => { add(kv, "span", k); add(kv, "span", String(v)); };
  row("Title", rep.title || "(not set)");
  row("Records verified", rep.records);
  row("Newest record", "#" + rep.head_seq);
  row("Newest fingerprint", rep.head_hash || "-");
  row("Chain", rep.chain_ok ? "intact" : "BROKEN");
  row("Signed checkpoints", rep.checkpoints.matching + " of " + rep.checkpoints.total + " match");
  row("Signatures checked", rep.checkpoints.signatures_checked ? "yes" : "NO (no key configured)");
  row("Latest checkpoint", rep.checkpoints.latest_seq >= 0 ? "#" + rep.checkpoints.latest_seq + " at " + new Date(rep.checkpoints.latest_time).toLocaleString() : "none");
}

$("rerun").onclick = () => run().catch((e) => { $("result").textContent = "Error: " + e.message; });
run().catch((e) => { $("result").textContent = "Error: " + e.message; });

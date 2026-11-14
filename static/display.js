"use strict";

const box = document.getElementById("entries");
let last = -1;

function add(record) {
  if (record.seq <= last) return;
  last = record.seq;
  const card = document.createElement("article");
  card.className = "entry";
  const top = document.createElement("div");
  top.className = "top";
  top.textContent = "#" + String(record.seq).padStart(4, "0") + "  " + record.created_at + "  " + (record.first_name + " " + record.last_name).trim();
  const body = document.createElement("p");
  body.className = "body";
  body.textContent = record.body;
  card.append(top, body);
  box.prepend(card);
}

async function start() {
  const state = await (await fetch("api/state")).json();
  document.getElementById("title").textContent = state.title || "CapLog";
  const rows = await (await fetch("api/records?limit=20")).json();
  rows.forEach(add);
  const source = new EventSource("api/events?after=" + last);
  source.addEventListener("record", (event) => add(JSON.parse(event.data)));
  document.getElementById("help-btn").onclick = async () => {
    const panel = document.getElementById("help");
    if (!panel.hidden) { panel.hidden = true; return; }
    panel.innerHTML = await (await fetch("/static/help/user.html")).text();
    panel.hidden = false;
  };
}

start();

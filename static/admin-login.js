"use strict";

const $ = (id) => document.getElementById(id);

function readCsrf() {
  const match = document.cookie.match(/(?:^|; )caplog_csrf=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-msg").textContent = "";
  try {
    const res = await fetch("/admin/login", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CapLog": "1",
        "X-CSRF": readCsrf(),
      },
      body: JSON.stringify({ username: $("user").value.trim(), password: $("pass").value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    location.href = "/admin";
  } catch (err) {
    $("login-msg").textContent = err.message;
  }
});

$("help-btn").onclick = async () => {
  const panel = $("help");
  if (!panel.hidden) { panel.hidden = true; return; }
  panel.innerHTML = await (await fetch("/static/help/admin.html")).text();
  panel.hidden = false;
};

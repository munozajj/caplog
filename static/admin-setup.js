"use strict";

const $ = (id) => document.getElementById(id);

function readCsrf() {
  const match = document.cookie.match(/(?:^|; )caplog_csrf=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
}

$("setup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("setup-msg").textContent = "";
  const title = $("title").value.trim();
  if (!title) return;
  const ok = window.confirm("The title is permanent and can never be changed. The admin account cannot edit or delete entries.\n\n" + title);
  if (!ok) return;
  try {
    const res = await fetch("/admin/setup", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CapLog": "1",
        "X-CSRF": readCsrf(),
      },
      body: JSON.stringify({
        title,
        username: $("setup-user").value.trim(),
        first_name: $("setup-first").value.trim(),
        last_name: $("setup-last").value.trim(),
        password: $("setup-pass").value,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    location.href = "/admin/login";
  } catch (err) {
    $("setup-msg").textContent = err.message;
  }
});

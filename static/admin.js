"use strict";

const $ = (id) => document.getElementById(id);
let csrf = "";
let admin = null;

const LABELS = {
  post_mode: "Who can post",
  users_may_change_display: "Users may change display settings",
  site_fqdn: "Site name (FQDN)",
  https_enabled: "Serve HTTPS",
  https_port: "HTTPS port",
  http_redirect: "Redirect HTTP to HTTPS",
  cert_warn_days: "Warn this many days before a certificate expires",
  ntp_enabled: "Compare the clock to an NTP server",
  ntp_server: "NTP server",
  ntp_threshold: "Clock warning threshold (seconds)",
  max_entry_length: "Maximum entry length",
  accent_color: "Accent color",
  show_ips: "Show IP addresses",
  guest_name_rules: "Require simple guest names",
  feature_search: "Search and filters",
  feature_tags: "Tags",
  feature_notice: "Pinned notice",
  feature_corrections: "Correction entries",
  feature_rate_limit: "Post rate limit",
  feature_scheduled_export: "Scheduled export",
  feature_display_page: "Display page",
  show_number: "Default: show entry number",
  show_name: "Default: show name",
  show_time: "Default: show date and time",
  show_ip: "Default: show IP address",
  show_poster: "Default: show Account or Guest",
  show_tags: "Default: show tags",
  time_zone: "Default time zone",
  clock: "Default clock",
  post_box: "Default post box position",
  notice: "Pinned notice",
  tags: "Tags, separated by commas",
  rate_limit_count: "Posts allowed in the window",
  rate_limit_window: "Rate limit window (seconds)",
  login_max_failures: "Failed sign-ins before lockout",
  login_lock_minutes: "Lockout minutes",
  export_every: "Export schedule",
  export_pdf: "Also write a PDF backup",
  export_keep: "Backup files to keep",
};

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CapLog": "1", "X-CSRF": csrf },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function field(key, value) {
  const wrap = document.createElement("div");
  const label = document.createElement("label");
  label.textContent = LABELS[key] || key;
  label.htmlFor = "s-" + key;
  wrap.append(label);
  let input;
  if (value === "true" || value === "false") {
    input = document.createElement("select");
    ["true", "false"].forEach((item) => input.append(new Option(item, item)));
  } else if (key === "post_mode") {
    input = document.createElement("select");
    ["both", "guests", "accounts"].forEach((item) => input.append(new Option(item, item)));
  } else if (key === "post_box") {
    input = document.createElement("select");
    ["top", "bottom", "left", "right", "center"].forEach((item) => input.append(new Option(item, item)));
  } else if (key === "time_zone") {
    input = document.createElement("select");
    ["local", "utc"].forEach((item) => input.append(new Option(item, item)));
  } else if (key === "clock") {
    input = document.createElement("select");
    ["24", "12"].forEach((item) => input.append(new Option(item, item)));
  } else if (key === "export_every") {
    input = document.createElement("select");
    ["off", "hourly", "daily"].forEach((item) => input.append(new Option(item, item)));
  } else if (key === "notice") {
    input = document.createElement("textarea");
  } else {
    input = document.createElement("input");
  }
  input.id = "s-" + key;
  input.value = value;
  wrap.append(input);
  return wrap;
}

function draw(data) {
  admin = data;
  $("gate").hidden = true;
  $("panel").hidden = false;
  const form = $("settings-form");
  form.textContent = "";
  const title = document.createElement("h2");
  title.textContent = "Settings";
  form.append(title);
  Object.keys(data.settings).forEach((key) => {
    if (key === "export_last") return;
    form.append(field(key, data.settings[key]));
  });
  const row = document.createElement("div");
  row.className = "form-row";
  const button = document.createElement("button");
  button.className = "btn btn-add";
  button.type = "submit";
  button.textContent = "Save settings";
  const msg = document.createElement("span");
  msg.id = "set-msg";
  msg.className = "msg";
  row.append(button, msg);
  form.append(row);
  const list = $("users");
  list.textContent = "";
  data.users.forEach((user) => {
    const line = document.createElement("div");
    line.className = "form-row";
    line.append(document.createTextNode(user.username + " (" + user.role + (user.enabled ? "" : ", disabled") + (user.is_master ? ", first admin" : "") + ")"));
    if (!user.is_master) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "btn";
      toggle.textContent = user.enabled ? "Disable" : "Enable";
      toggle.onclick = () => act({ action: "enabled", username: user.username, enabled: !user.enabled });
      const role = document.createElement("button");
      role.type = "button";
      role.className = "btn";
      role.textContent = user.role === "admin" ? "Make user" : "Make admin";
      role.onclick = () => act({ action: "role", username: user.username, role: user.role === "admin" ? "user" : "admin" });
      line.append(toggle, role);
    }
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "btn";
    reset.textContent = "Reset password";
    reset.onclick = async () => {
      const password = prompt("New password for " + user.username + ". It is shown only this once and is not written in the log.");
      if (!password) return;
      await act({ action: "password", username: user.username, password });
      alert("Password updated. Give it to that person now. It will not be shown again.");
    };
    line.append(reset);
    list.append(line);
  });
  const cert = data.certificate || {};
  $("cert-info").textContent = cert.present
    ? "Expires " + cert.expires + (cert.warning ? ". " + cert.warning : ".")
    : "No certificate has been saved.";
}

async function act(body) {
  await post("api/admin/users", body);
  draw(await (await fetch("api/admin/state")).json());
}

async function load() {
  const state = await (await fetch("api/state")).json();
  csrf = state.csrf;
  const res = await fetch("api/admin/state");
  if (res.status === 403) return;
  draw(await res.json());
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await post("api/login", { username: $("user").value.trim(), password: $("pass").value });
    await load();
  } catch (err) {
    $("login-msg").textContent = err.message;
  }
});

$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = $("set-msg");
  msg.textContent = "";
  try {
    for (const input of $("settings-form").querySelectorAll("input, select, textarea")) {
      const key = input.id.slice(2);
      if (admin.settings[key] !== input.value) await post("api/admin/settings", { key, value: input.value });
    }
    draw(await (await fetch("api/admin/state")).json());
    msg.textContent = "Saved.";
  } catch (err) {
    msg.textContent = err.message;
  }
});

$("user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await act({
      action: "create",
      username: $("new-user").value.trim(),
      first_name: $("new-first").value.trim(),
      last_name: $("new-last").value.trim(),
      password: $("new-pass").value,
      role: $("new-role").value,
    });
    $("user-form").reset();
  } catch (err) {
    $("user-msg").textContent = err.message;
  }
});

async function saveCert(body) {
  const result = await post("api/admin/certificate", body);
  alert((result.warning ? result.warning + " " : "") + "Saved. Restart CapLog before HTTPS uses this certificate. Expires " + result.expires);
  draw(await (await fetch("api/admin/state")).json());
}

$("cert-generate").onclick = () => saveCert({ action: "generate", hostname: admin.settings.site_fqdn }).catch((err) => alert(err.message));
$("cert-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveCert({ action: "upload", certificate: $("cert-pem").value, key: $("key-pem").value }).catch((err) => alert(err.message));
});

$("help-btn").onclick = async () => {
  const panel = $("help");
  if (!panel.hidden) { panel.hidden = true; return; }
  panel.innerHTML = await (await fetch("/static/help/admin.html")).text();
  panel.hidden = false;
};

load().catch((err) => { document.body.textContent = err.message; });

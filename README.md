# CapLog

A shared logbook. Many people can add entries at the same time. Accounts are optional. An entry cannot be edited or deleted, including by an admin.

One logbook per deployment. Open it by name or by IP on port 8080. HTTPS, when enabled, uses port 8443.

## What it is for

CapLog is a record people write while work is in progress, and then keep. It is a general-purpose logbook. It is not limited to one kind of work.

Deploy it, then open `/admin` and finish the first-time setup. That page creates the title and the master admin account in the same step. The logbook page does not link to `/admin`. Until setup is finished, `/` shows only "This logbook has not been configured by the administrator yet." and the user API refuses posts. After that, guests can add entries, accounts can add entries, or both, depending on `post_mode`. After an entry is saved, no one can change it and no one can delete it. The text, the name, the time, and the address it came from stay as they were written. That is the history, and it is also the archive.

Several people can keep the same page open. Each person writes what they just did. The new entry appears for everyone, with the newest entry at the top. The list is in order, so you can see who did what, and what was written immediately before a problem.

One example is a network team making infrastructure changes. The team keeps the log open. Each person writes what they just changed, so everyone sees it. When something breaks, the log shows who did what, in order, right before it broke. That is only an example of how a team can use the log. The logbook itself is not specific to networking or to information technology.

- The title is typed once, at `/admin`, together with the master admin account, and cannot be changed. Setup can be completed only once.
- A guest enters a first name and a last name. A signed-in account is not asked for a name. The name comes from the account. Each entry shows Account and the username, or Guest.
- Each entry has up to `max_entry_length` characters of text. The default is 500. CapLog adds the entry number, the time (UTC, shown in the viewer's local time unless the viewer chooses UTC), and the poster's IP address.
- New entries show up for everyone immediately (Server-Sent Events). Newest entries are at the top.
- The closed entry shows the number, the date and time, the name, and the first line. Opening it shows the full text, the IP address, the entry type, and the hash. Expand all and Collapse all apply to the list.
- Every record is linked to the one before it by a SHA-256 fingerprint. Checkpoints of that chain are signed with a key the database never sees.
- Export PDF or JSON. Each export writes a permanent system entry. A JSON export can be checked on its own and imported into another CapLog.
- Help is on the `?` button. It is bundled with the page and works with no network. The user help and the admin help describe the same behavior as this README.

## Install Docker

Install Docker before the deploy steps. When it is running, both of these commands must succeed:

```bash
docker version
docker compose version
```

You also need Git. If `git` is not installed, use [the Git downloads page](https://git-scm.com/downloads).

### macOS

Install [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/). The current macOS release and the two before it are supported. The computer needs at least 4 GB of memory.

1. Download `Docker.dmg` from that page.
2. Open it and drag Docker to the Applications folder.
3. Open Docker from Applications and accept the agreement.
4. Wait until Docker Desktop says it is running.

```bash
docker version
docker compose version
```

### Windows

Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/). It uses the WSL 2 backend. In PowerShell, `wsl --version` must show WSL 2.1.5 or later. Turn on hardware virtualization if it is off.

1. Download `Docker Desktop Installer.exe` from that page.
2. In PowerShell, from the folder that contains the installer:

```powershell
Start-Process '.\Docker Desktop Installer.exe' -Wait -ArgumentList 'install', '--user'
```

3. Open Docker Desktop and accept the agreement. Leave it running.

```powershell
docker version
docker compose version
```

If you do the rest of the setup inside a WSL distribution, use the Linux commands in that distribution instead of the PowerShell deploy commands below.

### Linux

Install Docker Engine and the Compose plugin. Steps for distributions other than Ubuntu are on [the Docker Engine install page](https://docs.docker.com/engine/install/). The package list must include `docker-compose-plugin`.

On Ubuntu, the current [Docker Engine steps](https://docs.docker.com/engine/install/ubuntu/) are:

```bash
sudo apt remove $(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc | cut -f1)
sudo apt update
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Skip the `apt remove` line if none of those older packages are installed. Log out and log back in so the group change applies. You can run `newgrp docker` instead of logging out. Then:

```bash
docker version
docker compose version
```

## Deploy with Docker Compose

```bash
git clone https://github.com/munozajj/caplog.git
cd caplog
```

### macOS and Linux

```bash
cp .env.example .env
```

Edit `.env`. Set three passwords. Use letters and digits only. Or fill the three passwords with:

```bash
# macOS
for v in POSTGRES_PASSWORD CAPLOG_OWNER_PASSWORD CAPLOG_APP_PASSWORD; do sed -i '' "s/^$v=.*/$v=$(openssl rand -hex 16)/" .env; done

# Linux
for v in POSTGRES_PASSWORD CAPLOG_OWNER_PASSWORD CAPLOG_APP_PASSWORD; do sed -i "s/^$v=.*/$v=$(openssl rand -hex 16)/" .env; done
```

```bash
docker compose build
mkdir -p secrets
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/secrets:/out" caplog:local keygen -out /out/caplog_signing.key
chmod 0444 secrets/caplog_signing.key
docker compose up -d
```

The container runs as uid 65532 and must be able to read the key file.

### Windows (PowerShell)

```powershell
Copy-Item .env.example .env
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$names = "POSTGRES_PASSWORD","CAPLOG_OWNER_PASSWORD","CAPLOG_APP_PASSWORD"
$text = Get-Content .env
foreach ($name in $names) {
  $bytes = New-Object byte[] 16
  $rng.GetBytes($bytes)
  $pw = ([System.BitConverter]::ToString($bytes)).Replace("-","").ToLower()
  $text = $text -replace "^$name=.*", "$name=$pw"
}
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines("$PWD\.env", $text, $utf8)
docker compose build
New-Item -ItemType Directory -Force -Path secrets | Out-Null
docker run --rm -v "${PWD}/secrets:/out" caplog:local keygen -out /out/caplog_signing.key
docker run --rm --user 0 --entrypoint chmod -v "${PWD}/secrets:/out" caplog:local 0444 /out/caplog_signing.key
docker compose up -d
```

The container runs as uid 65532 and must be able to read the key file. PowerShell has no `chmod`, so the second `docker run` sets that mode.

`keygen` writes `secrets/caplog_signing.key` (mode 0600) and `secrets/caplog_signing.key.pub`. It refuses to overwrite an existing file. The `chmod` step then makes the private key readable by the container user. Copy the `.pub` file off this computer. The private key stays in `secrets/` and is passed to the container as a Docker secret. It is not in the image.

Open `http://localhost:8080/admin` to create the logbook title and the master admin account. Until that is done, `http://localhost:8080/` shows only "This logbook has not been configured by the administrator yet." The logbook page does not link to `/admin`. After setup, sign in at `http://localhost:8080/admin/login`.

To use another host port, set `CAPLOG_PORT` in `.env`. HTTPS, when the admin turns it on, is published as `CAPLOG_HTTPS_PORT` (default 8443). To listen on one address only, set `CAPLOG_BIND`.

Postgres is on an internal Compose network. It has no published port. Only the app container can reach it. The app is also on the non-internal `web` network, which is what publishes the host ports.

Certificate files are stored on the `caplog-certs` volume (`CAPLOG_CERTS`, default `/certs`). Scheduled backups are stored on the `caplog-backups` volume (`CAPLOG_BACKUPS`, default `/backups`). Neither volume is in the image or the repository.

Container DNS is not set in `docker-compose.yml`. To use specific resolvers, add a `dns:` list on the app service. Create an A record or a CNAME record for `site_fqdn` at your DNS host. CapLog does not run a DNS server.

## Signing key

Signed checkpoints are what catch someone who rewrites the database and recalculates every fingerprint.

- Create a key: `caplog keygen -out caplog_signing.key`
- Give the app the private key with `CAPLOG_SIGNING_KEY`.
- Keep a copy of the public key off the host. `caplog verify` can use `CAPLOG_PUBLIC_KEY` when the private key is not available.
- If no key is set, CapLog still runs. It logs a warning, and the Verify page says signatures are not being checked.

A checkpoint is written at startup, after every 25 new records (`CAPLOG_CHECKPOINT_EVERY`), every 10 minutes if anything changed (`CAPLOG_CHECKPOINT_INTERVAL`), and at shutdown. Each line is `{seq, hash, time, key_id, sig}` in `CAPLOG_CHECKPOINTS`, on its own volume.

## Verify

```bash
docker compose exec app caplog verify

# A JSON export on its own
caplog verify --file caplog-export.json

# A database, with the public key
caplog verify --database-url "$DATABASE_URL" \
  --checkpoints /checkpoints/checkpoints.jsonl \
  --public-key caplog_signing.key.pub
```

The exit code is 0 when the check passes and 2 when a problem is found. Add `--json` for the full report.

The Verify page is `/verify`. It shows OK, or the first bad record.

## Kubernetes (Helm)

The chart is `deploy/helm/caplog`. It runs one app pod and, unless you turn it off, one Postgres pod.

```bash
caplog keygen -out caplog_signing.key
kubectl create namespace caplog
kubectl -n caplog create secret generic caplog-signing-key \
  --from-file=signing.key=caplog_signing.key

kubectl -n caplog create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=munozajj \
  --docker-password=<a PAT with read:packages>

helm install caplog deploy/helm/caplog -n caplog \
  --set signingKey.existingSecret=caplog-signing-key \
  --set 'imagePullSecrets={ghcr-pull}' \
  --set ingress.enabled=true --set ingress.host=caplog.lab.example.com
```

On install, the chart creates a Secret with the database passwords if you do not set `database.auth.existingSecret`. Upgrade keeps the passwords already stored in that Secret.

To use a Postgres server you already run:

```bash
kubectl -n caplog create secret generic caplog-db \
  --from-literal=DATABASE_URL='postgresql://caplog_app:PASSWORD@db.example:5432/caplog' \
  --from-literal=CAPLOG_MIGRATE_URL='postgresql://caplog_owner:PASSWORD@db.example:5432/caplog'

helm install caplog deploy/helm/caplog -n caplog \
  --set database.enabled=false \
  --set externalDatabase.existingSecret=caplog-db \
  --set externalDatabase.cidr=10.1.0.0/24 \
  --set signingKey.existingSecret=caplog-signing-key
```

The owner role must be allowed to create the tables. The app role may `SELECT` and `INSERT` on `records` only. It may not `UPDATE` or `DELETE` a record. It may `SELECT`, `INSERT`, and `UPDATE` on `users` and `settings` (not `DELETE` on users). It may `SELECT`, `INSERT`, and `DELETE` on `sessions`, `login_attempts`, and `post_hits`. It may `SELECT` and `INSERT` on `correction_links`. `caplog migrate` (run at startup when `CAPLOG_MIGRATE_URL` is set) creates the tables, the triggers, and those grants.

The NetworkPolicy allows inbound traffic to port 8080, and to the HTTPS port only when `https.enabled` is true. It allows app-to-Postgres on port 5432, and DNS. UDP port 123 is allowed only when `timeCheck.enabled` is true. That chart value is separate from the admin setting `ntp_enabled`. Both are required before the clock check can leave the cluster. Other egress is denied. Set `externalDatabase.cidr` when Postgres is outside the cluster, or the app cannot open that connection.

If an Ingress is in front of CapLog, leave in-app TLS off and set `trustedProxies` to that proxy's IP or CIDR. Otherwise the stored address is the proxy, which is what you want unless you trust the proxy. Live updates need buffering turned off on the proxy (`X-Accel-Buffering: no` is set on the event stream). Certificate and backup volumes are created by the chart (`persistence.certs` and `persistence.backups`). The chart version is 0.3.0 and `appVersion` is 2.1.0. Build and push that image, or set `image.tag`, before you install this chart.

## Using it

- Open `/admin` once to set the title and the master admin account. After that, `/admin` sends a signed-out visitor to `/admin/login`. The title cannot be changed. The first admin cannot be disabled or changed to a normal user.
- The logbook page shows the title, the post box, and the entries. Search, filters, display settings, export, import, expand all, collapse all, and sign-in start inside the Menu control. The `?` button stays visible. The example entry stays behind Show example.
- A guest adds an entry with a first name, a last name, and the text. A signed-in account is not asked for a name. The counter uses the same character count as the server. Add asks you to confirm: "This entry is permanent and can never be edited or deleted."
- An example entry sits behind Show example, under the post box. The text is "The west door was locked and the lights were left on for the next person." It is not stored.
- The post box position (`post_box`) can be top, bottom, left, right, or center. The entries use the remaining space, so the post box does not sit on top of them. That choice is display only.
- Times are stored in UTC. The page can show local time or UTC, and a 12-hour or 24-hour clock. The stored UTC value is the tooltip.
- Export PDF or Export JSON downloads the log. A system entry is written first and is included in the file. `site_fqdn` is included in the PDF header when it is set.
- Import reads a CapLog JSON export, checks its chain, and appends the records. Nothing already in the log is changed. Lowering `max_entry_length` does not reject an older export whose entries are within 500 characters.
- Verify checks the chain and the checkpoint signatures.

## Accounts

Accounts are stored in this deployment only. There is no external identity provider. Passwords are hashed with argon2id (`argon2-cffi`). The logbook session cookie `caplog_session` is HttpOnly and SameSite=Lax, and its path is `/`. The admin session cookie `caplog_admin` is HttpOnly and SameSite=Lax, and its path is `/admin`, so the browser sends it only to `/admin`. Signing in on the logbook does not open `/admin`. Signing in at `/admin/login` does not sign you in on the logbook. Only an account with the admin role can complete the admin sign-in. A cookie is marked Secure only when the request is already HTTPS. Sessions last 12 hours. Sign-in is locked after `login_max_failures` failures in `login_lock_minutes` (defaults 5 and 15). Admin sign-ins and admin sign-in failures use that same lock. The lock applies to that username and to that IP address.

Every form post also sends `X-CapLog: 1` and `X-CSRF`. The CSRF token is the cookie `caplog_csrf`, which is not HttpOnly, and the header `X-CSRF` must match it.

`post_mode` is `both`, `guests`, or `accounts`. The default is `both`. Disabling an account does not remove or hide that person's entries. An admin can create accounts, enable or disable them, reset a password, and set the role to admin or user. The new password is not written into the log.

When `guest_name_rules` is true, a guest name is required, 1 to 60 characters, and may contain only letters, spaces, apostrophes, and hyphens. When it is false, only the length is checked, up to 100 characters.

## Admin

`/admin` is the only admin address. The logbook page does not link to it. Before setup, `/admin` is the setup form. After setup, a signed-out visit to `/admin` redirects to `/admin/login`. Admin API routes are under `/admin/api/`. A call without an admin session returns HTTP 401. The page states that an admin cannot edit, hide, or delete an entry. Every settings change and every account change is a permanent system entry. The name on that entry is CapLog System. The body names the setting and the new value. It does not contain passwords, session tokens, or certificate private keys.

These settings are stored in the database. Display choices do not change a stored record.

| Setting | Default | What it does |
|---|---|---|
| `post_mode` | `both` | `guests`, `accounts`, or `both` |
| `users_may_change_display` | `true` | Whether a person may change the display, including `post_box` |
| `site_fqdn` | empty | Host name used in links, exports, and the PDF header |
| `https_enabled` | `false` | Listen for HTTPS after a restart, if the certificate files exist |
| `https_port` | `8443` | Public HTTPS port used in the HTTP redirect |
| `http_redirect` | `false` | Redirect HTTP to HTTPS when `https_enabled` is also true. `/healthz` stays on HTTP |
| `cert_warn_days` | `30` | Warn when the certificate expires inside this many days |
| `ntp_enabled` | `false` | Compare the clock. CapLog does not set the clock |
| `ntp_server` | empty | NTP server name. The Helm value `timeCheck.enabled` must also be true |
| `ntp_threshold` | `2` | Show a banner when the drift is larger than this many seconds |
| `max_entry_length` | `500` | Maximum characters in a new entry |
| `accent_color` | `#0d6e6a` | Accent color, six hex digits |
| `show_ips` | `true` | Whether IP addresses can be shown. They are still stored |
| `guest_name_rules` | `true` | Restrict guest names as described above |
| `feature_search` | `true` | Search by name, date range, text, and tag, and jump to an entry number |
| `feature_tags` | `true` | Admin-defined tags chosen when posting |
| `feature_notice` | `true` | Pinned notice. Setting or clearing `notice` is a system entry |
| `feature_corrections` | `true` | A new entry linked to an older one. The original is not changed |
| `feature_rate_limit` | `false` | Limit posts per IP address and per account |
| `feature_scheduled_export` | `false` | Write JSON backups, and a PDF when `export_pdf` is true |
| `feature_display_page` | `true` | Read-only `/display` page, large text, newest first, no post box |
| `show_number`, `show_name`, `show_time`, `show_ip`, `show_poster`, `show_tags` | `true` | Display defaults |
| `time_zone` | `local` | `local` or `utc` |
| `clock` | `24` | `12` or `24` |
| `post_box` | `top` | `top`, `bottom`, `left`, `right`, or `center` |
| `notice` | empty | The pinned notice text |
| `tags` | `note` | Comma-separated tag names |
| `rate_limit_count` | `30` | Posts allowed in the window |
| `rate_limit_window` | `600` | Window length in seconds |
| `login_max_failures` | `5` | Failed sign-ins before lockout |
| `login_lock_minutes` | `15` | Lockout length |
| `export_every` | `daily` | `off`, `hourly`, or `daily`. Nothing is written until `feature_scheduled_export` is true |
| `export_pdf` | `false` | Also write a PDF backup |
| `export_keep` | `7` | How many backup files to keep |
| `export_last` | empty | Set by the scheduled export. Not edited on the admin form |

A guest's display choices stay in the browser. An account's display choices stay on the account. Changing them does not write a system entry and does not change any record.

## Certificates, time, and DNS

Generate a self-signed certificate on the admin page, or upload a PEM certificate and an unencrypted PEM private key. The files are `cert.pem` (mode 0644) and `key.pem` (mode 0600) under `CAPLOG_CERTS`. The key is not stored in the database, the logs, or the repository. The admin page shows the expiry and a warning when it is inside `cert_warn_days`.

The HTTPS listener starts only when the process starts, `https_enabled` is true, and both files exist. Set `CAPLOG_HTTPS_PORT` to the same port as `https_port` (default 8443), then restart the container or the pod. Changing the files or `https_enabled` does not reload TLS in a running process. Leave `https_enabled` and `http_redirect` off when a reverse proxy or a Kubernetes Ingress terminates TLS.

CapLog only queries NTP (UDP port 123). It never sets the clock. The comparison is cached for 60 seconds. Run chrony or another NTP client on the Docker host, or on the Kubernetes nodes. The admin settings `ntp_enabled` and `ntp_server` choose the server. The Helm value `timeCheck.enabled` is what opens UDP 123. Both are required. A banner is shown when the drift is over `ntp_threshold`.

DNS is also outside CapLog. Use Compose `dns:` or the cluster DNS so the pod can resolve `ntp_server` and the database. Publish an A record or a CNAME record for `site_fqdn`.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `CAPLOG_LISTEN` | `:8080` | Address and port for HTTP |
| `DATABASE_URL` | (required) | App role connection string |
| `CAPLOG_MIGRATE_URL` | (none) | Owner connection string. If set, `serve` migrates before it listens |
| `CAPLOG_APP_ROLE` | `caplog_app` | Role that receives the grants described above |
| `CAPLOG_SIGNING_KEY` | (none) | Ed25519 private key, PEM |
| `CAPLOG_PUBLIC_KEY` | (none) | Public key for `verify` |
| `CAPLOG_CHECKPOINTS` | `/checkpoints/checkpoints.jsonl` | Checkpoint file |
| `CAPLOG_CHECKPOINT_EVERY` | `25` | Checkpoint after this many new records |
| `CAPLOG_CHECKPOINT_INTERVAL` | `10m` | Also checkpoint on this interval if anything changed |
| `CAPLOG_MAX_IMPORT_MB` | `20` | Largest import accepted |
| `CAPLOG_TRUSTED_PROXIES` | (empty) | Comma-separated IPs or CIDRs |
| `CAPLOG_CERTS` | `/certs` | Directory for `cert.pem` and `key.pem` |
| `CAPLOG_BACKUPS` | `/backups` | Directory for scheduled JSON and PDF backups |
| `CAPLOG_HTTPS_PORT` | `8443` | Container port for the HTTPS listener |

Every write request must send the header `X-CapLog: 1` and a matching `X-CSRF` header. That blocks a form post from another site.

## How permanence works (and its limits)

1. The application only inserts rows into `records`. There is no edit or delete function for an entry.
2. PostgreSQL triggers abort `UPDATE`, `DELETE`, and `TRUNCATE` on `records`.
3. Two roles. `caplog_owner` runs migrations and owns the tables. `caplog_app` has `INSERT` and `SELECT` on `records` only. It can update accounts and settings, and it can delete sessions, sign-in attempts, and rate-limit rows. It cannot delete a user or a record. The web process uses `caplog_app`.
4. Each record stores a SHA-256 fingerprint of the previous fingerprint and its own fields. See the encoding below.
5. Signed checkpoints record an old fingerprint. A rewritten chain cannot produce a matching signature without the private key.
6. The app container runs as a non-root user, with a read-only root filesystem and no Linux capabilities. The signing key is a secret, not a file in the image. Certificate keys stay on the certificate volume.

Someone with full control of the host can still destroy the data, or roll the database and the checkpoint file back together. They cannot change an existing entry without Verify failing, if you still have an older checkpoint or the public key somewhere they cannot reach.

## Hash encoding

`hash` is the SHA-256 hex of this text. The length is the number of UTF-8 bytes.

```text
caplog-v1
<len(seq)>:<seq>
<len(kind)>:<kind>
<len(created_at)>:<created_at>
<len(first_name)>:<first_name>
<len(last_name)>:<last_name>
<len(ip)>:<ip>
<len(body)>:<body>
<len(meta)>:<meta>
<len(prev_hash)>:<prev_hash>
```

Record 0 is the title (`kind` is `genesis`). Its `prev_hash` is 64 zeros. A checkpoint is signed over:

```text
caplog-checkpoint-v1
<seq>
<hash>
<time>
```

The JSON export:

```json
{
  "format": "caplog-export",
  "version": 1,
  "title": "…",
  "exported_at": "2026-01-01T00:00:00Z",
  "records": []
}
```

`kind` is `genesis`, `entry`, `export`, `import`, `imported`, or `system`. A system record is an admin or settings change. It is permanent, and it uses the same hash encoding as every other record.

## Tests

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/caplog pytest
```

The tests create `caplog_owner` and `caplog_app` in that server. The database user in `TEST_DATABASE_URL` must be allowed to create roles and databases.

## Layout

| Path | What it is |
|---|---|
| `AGENTS.md` | Instruction to check this README after every code change |
| `caplog/` | Chain, database, checkpoints, PDF, HTTP API, commands |
| `static/` | The page, the style, user help, admin help, the display page, and the Inter font |
| `deploy/helm/caplog` | Helm chart |
| `deploy/postgres/init-roles.sh` | Creates the two database roles |

The font files under `static/fonts/` are Inter 4.1, licensed under the SIL Open Font License. The license is `static/fonts/LICENSE.txt`.

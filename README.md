# CapLog

A shared logbook. Many people can add entries at the same time. There are no accounts, and an entry cannot be edited or deleted.

One logbook per deployment. Open it by name or by IP on port 8080.

## What it is for

CapLog is a record people write while work is in progress, and then keep.

Deploy it, finish the first-time setup, and give people the address. They do not need an account. Anyone who can open the page can add an entry. After an entry is saved, no one can change it and no one can delete it. The text, the name, the time, and the address it came from stay as they were written. That is the history, and it is also the archive.

Several people can keep the same page open. Each person writes what they just did. The new entry appears for everyone, with the newest entry at the top. The list is in order, so you can see who did what, and what was written immediately before a problem. The log is not tied to one kind of work.

- The title is typed once, on first use, and cannot be changed.
- Each entry has a first name, a last name, and up to 500 characters of text. CapLog adds the entry number, the time (UTC, shown in the viewer's local time), and the poster's IP address.
- New entries show up for everyone immediately (Server-Sent Events). Newest entries are at the top.
- Every record is linked to the one before it by a SHA-256 fingerprint. Checkpoints of that chain are signed with a key the database never sees.
- Export PDF or JSON. Each export writes a permanent system entry. A JSON export can be checked on its own and imported into another CapLog.

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

Open `http://localhost:8080/`. The first visit asks for the logbook title.

`keygen` writes `secrets/caplog_signing.key` (mode 0600) and `secrets/caplog_signing.key.pub`. It refuses to overwrite an existing file. The `chmod` step then makes the private key readable by the container user. Copy the `.pub` file off this computer. The private key stays in `secrets/` and is passed to the container as a Docker secret. It is not in the image.

To use another host port, set `CAPLOG_PORT` in `.env`. To listen on one address only, set `CAPLOG_BIND`.

Postgres is on an internal Compose network. It has no published port. Only the app container can reach it.

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

The owner role must be allowed to create the table. The app role needs `SELECT` and `INSERT` only. `caplog migrate` (run at startup when `CAPLOG_MIGRATE_URL` is set) creates the table, the triggers, and those grants.

The NetworkPolicy allows inbound traffic to port 8080, app-to-Postgres on port 5432, and DNS. Other egress is denied. Set `externalDatabase.cidr` when Postgres is outside the cluster, or the app cannot open that connection.

If an Ingress is in front of CapLog, set `trustedProxies` to that proxy's IP or CIDR. Otherwise the stored address is the proxy, which is what you want unless you trust the proxy. Live updates need buffering turned off on the proxy (`X-Accel-Buffering: no` is set on the event stream).

## Using it

- The first page sets the title. Confirm it. It cannot be changed.
- Add an entry with a first name, a last name, and the text. The counter uses the same character count as the server. Add asks you to confirm: "This entry is permanent and can never be edited or deleted."
- Times are stored in UTC and shown in your local time. The UTC value is the tooltip.
- Export PDF or Export JSON downloads the log. A system entry is written first and is included in the file.
- Import reads a CapLog JSON export, checks its chain, and appends the records. Nothing already in the log is changed.
- Verify checks the chain and the checkpoint signatures.

## How permanence works (and its limits)

1. The application only inserts rows. There is no edit or delete function.
2. PostgreSQL triggers abort `UPDATE`, `DELETE`, and `TRUNCATE` on `records`.
3. Two roles. `caplog_owner` runs migrations and owns the table. `caplog_app` has `INSERT` and `SELECT` only. The web process uses `caplog_app`.
4. Each record stores a SHA-256 fingerprint of the previous fingerprint and its own fields. See the encoding below.
5. Signed checkpoints record an old fingerprint. A rewritten chain cannot produce a matching signature without the private key.
6. The app container runs as a non-root user, with a read-only root filesystem and no Linux capabilities. The signing key is a secret, not a file in the image.

Someone with full control of the host can still destroy the data, or roll the database and the checkpoint file back together. They cannot change an existing entry without Verify failing, if you still have an older checkpoint or the public key somewhere they cannot reach.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `CAPLOG_LISTEN` | `:8080` | Address and port |
| `DATABASE_URL` | (required) | App role connection string |
| `CAPLOG_MIGRATE_URL` | (none) | Owner connection string. If set, `serve` migrates before it listens |
| `CAPLOG_APP_ROLE` | `caplog_app` | Role that receives `SELECT` and `INSERT` |
| `CAPLOG_SIGNING_KEY` | (none) | Ed25519 private key, PEM |
| `CAPLOG_PUBLIC_KEY` | (none) | Public key for `verify` |
| `CAPLOG_CHECKPOINTS` | `/checkpoints/checkpoints.jsonl` | Checkpoint file |
| `CAPLOG_CHECKPOINT_EVERY` | `25` | Checkpoint after this many new records |
| `CAPLOG_CHECKPOINT_INTERVAL` | `10m` | Also checkpoint on this interval if anything changed |
| `CAPLOG_MAX_IMPORT_MB` | `20` | Largest import accepted |
| `CAPLOG_TRUSTED_PROXIES` | (empty) | Comma-separated IPs or CIDRs |

Every write request must send the header `X-CapLog: 1`. That blocks a form post from another site.

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

`kind` is `genesis`, `entry`, `export`, `import`, or `imported`.

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
| `static/` | The page, the style, and the Inter font |
| `deploy/helm/caplog` | Helm chart |
| `deploy/postgres/init-roles.sh` | Creates the two database roles |

The font files under `static/fonts/` are Inter 4.1, licensed under the SIL Open Font License. The license is `static/fonts/LICENSE.txt`.

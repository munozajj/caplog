# CapLog

A shared logbook. Many people can add entries at the same time. There are no accounts, and an entry cannot be edited or deleted.

One logbook per deployment. Open it by name or by IP on port 8080.

- The title is typed once, on first use, and cannot be changed.
- Each entry has a first name, a last name, and up to 500 characters of text. CapLog adds the entry number, the time (UTC, shown in the viewer's local time), and the poster's IP address.
- New entries show up for everyone immediately (Server-Sent Events). Newest entries are at the top.
- Every record is linked to the one before it by a SHA-256 fingerprint. Checkpoints of that chain are signed with a key the database never sees.
- Export PDF or JSON. Each export writes a permanent system entry. A JSON export can be checked on its own and imported into another CapLog.

## Quick start (Docker Compose on a Mac)

You need Docker Desktop with the Compose plugin.

```bash
git clone https://github.com/munozajj/caplog.git
cd caplog
git checkout feat/python-fastapi
cp .env.example .env
```

Edit `.env`. Set three passwords. Use letters and digits only.

```bash
docker compose build

mkdir -p secrets
docker compose run --rm --no-deps --user "$(id -u):$(id -g)" \
  -v "$PWD/secrets:/out" app \
  keygen -out /out/caplog_signing.key

docker compose up -d
```

Open `http://localhost:8080/`. The first visit asks for the logbook title.

`keygen` writes `secrets/caplog_signing.key` (mode 0600) and `secrets/caplog_signing.key.pub`. It refuses to overwrite an existing file. Copy the `.pub` file off this Mac. The private key stays in `secrets/` and is passed to the container as a Docker secret. It is not in the image.

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
| `caplog/` | Chain, database, checkpoints, PDF, HTTP API, commands |
| `static/` | The page, the style, and the Inter font |
| `deploy/helm/caplog` | Helm chart |
| `deploy/postgres/init-roles.sh` | Creates the two database roles |

The font files under `static/fonts/` are Inter 4.1, licensed under the SIL Open Font License. The license is `static/fonts/LICENSE.txt`.

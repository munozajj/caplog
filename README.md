# CapLog

A shared logbook web app that looks and works like a green military logbook.

- **One logbook per deployment.** The title is typed in once, on first start, and can never be changed.
- **Anyone with the address can post.** No accounts, no logins. Each entry has a first name, last name and up to 500 characters of text. CapLog adds the entry number, time and the poster's IP address itself.
- **Permanent.** Entries can never be edited, hidden or deleted, not even by an admin. Before an entry is saved, a second confirmation warns that it's permanent.
- **Live.** New entries show up on everyone's screen right away (Server-Sent Events), with no refresh needed.
- **Tamper-evident.** Every record is fingerprinted and chained to the one before it, and the chain is regularly signed with a key the database never sees. A **Verify** page (and the `caplog verify` command) recomputes everything and points to exactly where any change happened.
- **Export / import.** PDF (a green-book printout) and JSON (every record and fingerprint, so it can be verified). Each export or import writes a permanent system entry recording it.
- **Small.** One ~13 MB static binary with SQLite built in, in a distroless container. It idles at about 15 MB of RAM, so you can run several copies.

---

## Quick start (Docker Compose)

You need Docker with the Compose plugin.

```bash
git clone https://github.com/munozajj/caplog.git && cd caplog
docker compose build

# 1. Create the signing key (one time). Keep the private key secret.
mkdir -p secrets
docker run --rm -v "$PWD/secrets:/out" --user "$(id -u):$(id -g)" caplog:local \
  keygen -out /out/caplog_signing.key
chmod 0444 secrets/caplog_signing.key   # the container runs as uid 65532 and must be able to read it

# 2. Start it
docker compose up -d
```

Open `http://<host-ip-or-name>:8080/`. On the first visit you get the setup page, where you enter the logbook title.

To change the port, or to listen on one address only, create a `.env` file next to `docker-compose.yml`:

```
CAPLOG_PORT=8081
CAPLOG_BIND=192.168.1.50
```

To run more than one logbook on the same host, copy the folder (or use `docker compose -p <name>`) and give each copy its own port. Each copy gets its own volumes.

**Copy `secrets/caplog_signing.key.pub` somewhere off this host** (a password manager, another server, or a printout). See [How permanence works](#how-permanence-works-and-its-limits) for why.

## Kubernetes (Helm)

The chart is in `deploy/helm/caplog`. It always runs **exactly one pod**, because SQLite allows only one writer and the gap-free entry numbering depends on it.

```bash
# 1. Make a key locally (or with Docker as above) and store it as a Secret
caplog keygen -out caplog_signing.key
kubectl create namespace caplog
kubectl -n caplog create secret generic caplog-signing-key --from-file=signing.key=caplog_signing.key

# 2. The image is private (private repo), so give the cluster a pull secret:
kubectl -n caplog create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=munozajj --docker-password=<a PAT with read:packages>

# 3. Install
helm install caplog deploy/helm/caplog -n caplog \
  --set signingKey.existingSecret=caplog-signing-key \
  --set 'imagePullSecrets={ghcr-pull}' \
  --set ingress.enabled=true --set ingress.host=caplog.lab.example.com
```

The chart provides:
- a Deployment with replicas fixed at 1, a Recreate update strategy, non-root, read-only root filesystem, all capabilities dropped and seccomp `RuntimeDefault`
- two PersistentVolumeClaims, one for the database and one for checkpoints. They're kept if you uninstall the chart.
- a Service, plus an optional Ingress for a hostname (FQDN)
- a NetworkPolicy that only allows traffic to the app port and blocks all outbound traffic
- small resource requests and limits (10m CPU / 24Mi requested, 500m / 96Mi limit)

See `values.yaml` for every option.

## Reaching it by name or IP / reverse proxies

CapLog listens on `CAPLOG_LISTEN` (default `:8080`, all addresses). It works directly by IP, or behind a reverse proxy or Ingress under any hostname. All links in the page are relative, so it works under a sub-path too, as long as the proxy strips the prefix.

Live updates use a long-lived HTTP connection. If you use **nginx** in front, turn buffering off for it (CapLog also sends `X-Accel-Buffering: no`, which nginx honours):

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

**Recommendation:** keep CapLog on your local network or behind a VPN like Tailscale rather than exposing it to the internet. Anyone who can reach it can post, and nothing posted can ever be removed.

## Using it

- **Post:** fill in first name, last name and the entry, then press **Enter** (or Submit). Shift+Enter adds a new line. A confirmation dialog shows the entry and warns that it's permanent. It's only saved when you confirm.
- **Times** are stored in UTC and shown in each viewer's local time. Hover over a time to see the UTC value.
- **Export JSON / Export PDF:** downloads the whole log. A permanent system entry such as *"Logbook exported as PDF (records #0-#42)"* is added first, and it's included in the export.
- **Import:** choose a CapLog JSON export, from this logbook or another one. CapLog checks the file's whole hash chain first and rejects it if anything in it was changed. Valid records are added **to the end** of this log with new entry numbers, marked **IMPORTED**, and they keep their original entry number, time, names, IP address, text and source logbook title. An "Import started" and an "Import finished" system entry are written around them. Nothing already in the log is touched.
- **Verify:** the Verify link (`/verify`) checks the whole log and the signed checkpoints and reports OK, or exactly where it breaks.

## Signing key

Signed checkpoints are what catch someone who rewrites the database and recalculates every fingerprint to hide it.

- Create a key pair: `caplog keygen -out caplog_signing.key`. This writes `caplog_signing.key` (private, mode 0600) and `caplog_signing.key.pub` (public). It refuses to overwrite an existing file.
- Give the app the private key through `CAPLOG_SIGNING_KEY`, as a Docker/Compose secret or a Kubernetes Secret. **Never** put it in the repo, an image, or the database.
- Keep a copy of the **public** key off the host.
- If no key is configured, CapLog still runs, but it logs a clear warning at startup and the Verify page warns that signatures aren't being checked.

Checkpoints are written after every 25 new records (`CAPLOG_CHECKPOINT_EVERY`), every 10 minutes if anything changed (`CAPLOG_CHECKPOINT_INTERVAL`), at startup and at shutdown. Each one is a JSON line `{seq, hash, time, key_id, sig}` appended to `CAPLOG_CHECKPOINTS`, which lives on its own volume.

## Verifying from the command line

```bash
# Docker Compose (the image has no shell, but the caplog binary is there)
docker compose exec caplog /caplog verify

# Kubernetes
kubectl -n caplog exec deploy/caplog -- /caplog verify

# Off the host, using copies of the database, checkpoints and PUBLIC key
caplog verify -db caplog.db -checkpoints checkpoints.jsonl -public-key caplog_signing.key.pub

# A JSON export on its own
caplog verify -file caplog-export.json
```

The exit code is 0 when everything checks out and 2 when a problem is found. Add `-json` for a full report.

## How permanence works (and its limits)

Several layers, each covering a different kind of attack:

1. **No delete or edit feature.** The app's code only ever adds records. There is no code path that updates or deletes.
2. **Database triggers.** The SQLite database itself refuses any `UPDATE` or `DELETE` on the records table, even from the `sqlite3` command line, and refuses inserts that don't continue the chain properly.
3. **Hash chain.** Each record stores a SHA-256 fingerprint of its entry number, type, time, names, IP address, text, import details and **the previous record's fingerprint**. Record #0 holds the title. Changing any record breaks its own fingerprint and the link from the next record, and Verify reports the exact record.
4. **Signed checkpoints.** Someone could drop the triggers, edit a record, and recalculate every fingerprint after it so the chain looks consistent again. But the signed checkpoints still record the *old* fingerprints, and new matching ones can't be made without the private key. Checkpoints also catch records being deleted off the end (a rolled-back database).
5. **Locked-down container.** It's a distroless image with no shell or package manager, runs as non-root with a read-only root filesystem and no Linux capabilities, and the only writable places are the two volumes.

**Honest limits:** someone with root on the host (or cluster-admin) can still **destroy** the data: delete the volumes, or roll back the database *and* the checkpoint file together. What they **can't** do is change existing entries without Verify catching it. The protection is strongest when copies of the checkpoint file and the public key are kept somewhere they can't reach, such as a backup server, another machine, or a printed fingerprint from a PDF export. Then even a full rollback on the host shows up when you verify against those copies.

## Privacy

- The IP address of the device that adds an entry is stored on that entry and is permanent (it is part of the hash chain). Title, export and import system notes leave the IP empty. Imported records keep their original IP, if the file had one.
- By default CapLog records the connecting address. If you run it behind a reverse proxy or Ingress, that will be the proxy's IP unless you set `CAPLOG_TRUSTED_PROXIES` to that proxy's IP or CIDR, in which case CapLog will use `X-Forwarded-For` or `X-Real-IP`. Leave this unset unless you trust the proxy; otherwise a client could spoof those headers.
- User agents and cookies are not stored. There is no request log. The app only logs startup, checkpoints, imports and errors, never any client details.
- Names are whatever people type in. They're part of the permanent record.

## Settings

Every setting can be given as an environment variable or a flag (`caplog serve -h`).

| Variable | Default | What it does |
|---|---|---|
| `CAPLOG_LISTEN` | `:8080` | Address and port to listen on |
| `CAPLOG_DB` | `/data/caplog.db` | SQLite database file |
| `CAPLOG_SIGNING_KEY` | *(none)* | Private signing key file (PEM). Without it, checkpoints are off. |
| `CAPLOG_PUBLIC_KEY` | *(none)* | Public key, used by `verify` when the private key isn't available |
| `CAPLOG_CHECKPOINTS` | `/checkpoints/checkpoints.jsonl` | Signed checkpoint file |
| `CAPLOG_CHECKPOINT_EVERY` | `25` | Checkpoint after this many new records |
| `CAPLOG_CHECKPOINT_INTERVAL` | `10m` | Also checkpoint this often if anything changed |
| `CAPLOG_MAX_IMPORT_MB` | `20` | Largest import file accepted |
| `CAPLOG_TRUSTED_PROXIES` | *(none)* | Comma-separated IPs or CIDRs of reverse proxies. If the connecting address is in this list, the client IP is taken from `X-Forwarded-For` or `X-Real-IP`. Default is off. |

## Backups

- The simplest backup is **Export JSON**. It's verifiable on its own, and it can be imported into a fresh CapLog.
- For a full copy, stop the container and copy both volumes, or copy the database while it's running using a SQLite-aware tool.
- Copy `checkpoints.jsonl` and the public key off the host regularly.

## Building and testing from source

Requires Go (the version in `go.mod`). No C compiler is needed.

```bash
go test ./...                    # run all tests
go build -o caplog .             # build the binary
./caplog keygen -out dev.key
CAPLOG_DB=./dev.db CAPLOG_CHECKPOINTS=./dev-checkpoints.jsonl CAPLOG_SIGNING_KEY=./dev.key ./caplog serve
```

Code layout:

| Path | What's in it |
|---|---|
| `main.go` | Commands: `serve`, `keygen`, `verify`, `healthcheck`, `version` |
| `internal/logbook` | Database, hash chain, triggers, export/import, verification |
| `internal/checkpoint` | Signing keys and signed checkpoints |
| `internal/pdfexport` | The green-book PDF |
| `internal/web` | HTTP server, live updates, and the web page (`static/`, built into the binary) |
| `deploy/helm/caplog` | Helm chart |

## GitHub Actions

- **Every pull request and every push to `main`:** `go vet`, `go test`, `helm lint`, and a Docker build that isn't pushed anywhere.
- **Version tags only** (`git tag v0.1.0 && git push origin v0.1.0`): builds the image for amd64 and arm64 and pushes it to `ghcr.io/munozajj/caplog`.

To stay well inside the GitHub Pro allowance, runs are cancelled when you push again, images are only published for tags, and build caches are used. Each image is roughly 10 MB. Delete old package versions now and then to save storage.

## The JSON export format

```json
{
  "format": "caplog-export", "version": 1,
  "title": "…", "exported_at": "2026-01-01T00:00:00Z",
  "records": [
    {"seq": 0, "kind": "genesis", "created_at": "…", "first_name": "", "last_name": "", "ip": "",
     "body": "<title>", "meta": "", "prev_hash": "000…0", "hash": "…"}
  ]
}
```

`kind` is one of `genesis`, `entry`, `export`, `import` or `imported`. Here is how `hash` is calculated, so anyone can re-check it with their own tools. It's the SHA-256 (in hex) of the text `caplog-v1\n` followed by each of these fields in order: `seq`, `kind`, `created_at`, `first_name`, `last_name`, `ip`, `body`, `meta`, `prev_hash`. Each field is written as `<byte length>:<value>\n`.

# CapLog project memory

This file is the memory for the project. Read it before a task. Update Tasks, Decisions, and the Session log in the same pull request before finishing.

## 1. Goal

CapLog is a general-purpose shared logbook. People open it in a browser and write down what they just did. The entries stay in order and cannot be changed, so the log is both the live record and the archive. It is not built for one trade or one kind of work. One deployment is one logbook.

## 2. Hard rules

- Entries are permanent. No one can edit, hide, or delete an entry. That includes the master admin. The hash chain, the database triggers, the insert-and-select-only application role, the signed checkpoints, `/verify`, and the `X-CapLog` header stay in place.
- Every admin change and every settings change is written as a permanent system entry in the log. Do not store passwords, keys, or certificate material in that entry.
- Make the smallest change that does the task. Do not refactor unrelated code.
- Do not put secrets, keys, or passwords in the repository.

## 3. Decisions

- 2026-11-14: CapLog is one permanent shared logbook per deployment. The first version was Go, SQLite, Docker Compose, and Helm. [PR #1](https://github.com/munozajj/caplog/pull/1)
- 2026-11-14: Each entry stores the poster's IP address. [PR #2](https://github.com/munozajj/caplog/pull/2)
- 2026-11-14: The server is Python, FastAPI, and PostgreSQL. The deployment is two containers: the app and PostgreSQL. The hash text stays `caplog-v1`, so a JSON export from the Go version still verifies and imports. [PR #3](https://github.com/munozajj/caplog/pull/3)
- 2026-11-14: The page is a clean modern layout. The green-book page from the first version is not the current design. [PR #3](https://github.com/munozajj/caplog/pull/3)
- 2026-11-14: The repository is public.
- 2026-11-14: Permanence is a SHA-256 hash chain plus Ed25519 signed checkpoints. Triggers reject update, delete, and truncate of `records`. The application role can insert and select only.
- 2026-11-14: Deploy with Docker Compose and with the Helm chart. Both shipped with the first version and remain after the Python rewrite. [PR #1](https://github.com/munozajj/caplog/pull/1), [PR #3](https://github.com/munozajj/caplog/pull/3)
- 2026-11-14: On a new checkout, create the signing key with `docker run` before `docker compose up`, then set the key file mode to 0444 so the container user 65532 can read it. [PR #4](https://github.com/munozajj/caplog/pull/4)
- 2026-11-14: The app is on the internal `caplog` network and on a non-internal `web` network, so Docker publishes the app port. PostgreSQL stays only on `caplog`. [PR #5](https://github.com/munozajj/caplog/pull/5)

## 4. Tasks

### Done

- [x] Permanent entries, hash chain, database triggers, insert-and-select-only application role, signed checkpoints, `/verify`, and the `X-CapLog` header
- [x] Poster IP address on each entry
- [x] First and last name required before a post, and a second confirmation that the entry is permanent
- [x] Live updates, newest entry at the top, JSON and PDF export, JSON import
- [x] Python, FastAPI, and PostgreSQL, in two containers
- [x] Docker Compose and Helm, with the current container hardening
- [x] Clean modern page
- [x] Public repository
- [x] First-time signing-key steps that work on a fresh checkout
- [x] Published app port, with PostgreSQL only on the internal network
- [x] README install and deploy steps for macOS, Windows, and Linux

### Accounts and guests

- [ ] Local user accounts only (no external identity provider)
- [ ] Password hashing with a current algorithm (argon2id or bcrypt; not chosen yet)
- [ ] Sessions in secure HttpOnly SameSite cookies, and CSRF protection on all forms
- [ ] Guest (anonymous) posting
- [ ] First visit: create the master admin in the same step as the logbook title
- [ ] A logged-in user is not asked for a first and last name; the name comes from the account
- [ ] A guest must enter a first and last name before posting
- [ ] Each entry shows Account (with username) or Guest
- [ ] Disabling a user does not remove or hide that user's entries
- [ ] Login rate limit and lockout after repeated failed attempts

### Master admin (`/admin`)

- [ ] Posting mode: guests only, accounts only, or both
- [ ] User management: create, disable, enable, reset password, and set role (admin or user)
- [ ] The admin can turn every setting on or off, including whether users can see or change display settings, and whether each extra feature is enabled
- [ ] Site FQDN, used in links, exports, and the PDF header
- [ ] TLS: generate a self-signed certificate, or upload a PEM certificate and private key; show the expiry date and warn when it is near
- [ ] Certificate keys stored on a protected volume, never in the database, the logs, or the repository
- [ ] Option to serve HTTPS on a configurable port, and to redirect HTTP to HTTPS
- [ ] README: how TLS works behind a reverse proxy or a Kubernetes Ingress instead of in the app
- [ ] Time check: the admin enters an NTP server; the app compares its clock and shows a warning banner when the drift is over a configurable threshold (default 2 seconds)
- [ ] The app must not set the clock; the README explains time sync (chrony or NTP) on the host or on the Kubernetes nodes
- [ ] Helm NetworkPolicy allows outbound UDP 123 only when the time check is enabled
- [ ] README: container DNS is set in Compose (`dns:`) or in the cluster, and how to create an A or CNAME record for the FQDN

### Entry layout

- [ ] Each entry shows, in order: entry number, date and time, then the text, with the name and the Account or Guest marker on the same header line
- [ ] Each entry collapses. The closed view shows the number, time, name, and the first line of text
- [ ] Expanding an entry shows the full text, IP, entry type, and hash
- [ ] Expand all and Collapse all controls

### Logbook display controls

- [ ] Controls for what is shown: entry number, first and last name, date and time, IP, Account or Guest marker, and tags
- [ ] Display time zone (UTC or the viewer's local time) and a 12-hour or 24-hour clock
- [ ] The admin sets the defaults and decides whether users may change them
- [ ] A user's choices are saved in the browser, or on the account when the user is logged in
- [ ] These settings change only what is displayed, never the stored record

### Post box

- [ ] Under the post box, show an example of a finished post, with generic example text, using the same layout as a real entry

### Help

- [ ] A ? control on the user pages and on the admin space
- [ ] The help panel for that space: what CapLog is for, how to post, what each field and setting does, permanence and `/verify`, export and import
- [ ] Admin help also covers every admin setting, certificates, the time check, DNS, backups, and deployment
- [ ] Help is bundled with the app so it works offline, and it stays in step with the README

### Extra features (each one can be turned off by the admin)

- [ ] Search and filter by name, date range, text, and tag, plus a box to jump to an entry number
- [ ] Tags the admin defines, chosen when posting, with filtering by tag
- [ ] A pinned notice at the top; every change is logged
- [ ] Correction entries: a new entry linked to an older one, because nothing can be edited; both entries show the link
- [ ] Rate limit posts per IP and per account, with the limit set by the admin
- [ ] Scheduled JSON export, and an optional PDF, written to a backup volume, with a setting for how many to keep
- [ ] Read-only display page at `/display`: large text, newest entries, live updates, no post box
- [ ] Configurable maximum entry length (default 500), theme accent color, whether IP addresses are shown, and guest name rules

### README, deployment, and tests for the work above

- [ ] README "What it is for": keep it general, and add the example of a network team that keeps the log open, writes each change as it happens, and later reads who did what, in order, just before something broke
- [ ] Document every new feature, setting, and admin option in the README
- [ ] Update `docker-compose.yml`, `.env.example`, and the Helm chart for new volumes (certificates, backups), the HTTPS port, and new settings; keep the current hardening
- [ ] Tests: account login and lockout, guest and account posting modes, name not asked when logged in, admin-only routes, settings changes producing system entries, correction entries, rate limiting, display settings not changing stored records, and certificate upload validation
- [ ] Use current library versions and pin them

## 5. Open questions

- Password hash: argon2id or bcrypt. Not chosen.
- How a system entry is stored so the existing hash text stays valid: a new record kind, or an existing kind with the details in `meta`. Not chosen.
- Loading a new TLS certificate requires the server process to start again. That approach is not confirmed.
- Whether the first admin account can be disabled or changed to a normal user. Not chosen.
- The exact guest name rules (minimum length and allowed characters). Not chosen.
- The time check inside the app and the Helm rule for UDP port 123 are separate switches. How they stay in step is not chosen.
- Several logs with their own URLs, and a recovery key for the admin, were discussed earlier. They are not in the task list above.

## 6. Session log

- 2026-11-14: Added this project memory and the instruction to read it at the start of every task. No application code changed. [PR #6](https://github.com/munozajj/caplog/pull/6)
- 2026-11-14: Published the app port on a non-internal network and left PostgreSQL on the internal network only. [PR #5](https://github.com/munozajj/caplog/pull/5)
- 2026-11-14: Fixed the first-time signing-key steps, documented what the logbook is for, and added install steps for macOS, Windows, and Linux. [PR #4](https://github.com/munozajj/caplog/pull/4)
- 2026-11-14: Replaced the Go server with Python, FastAPI, and PostgreSQL, and kept the hash, export, and import behavior. [PR #3](https://github.com/munozajj/caplog/pull/3)
- 2026-11-14: Stored and showed the poster IP address on each entry. [PR #2](https://github.com/munozajj/caplog/pull/2)
- 2026-11-14: First working version: a permanent shared logbook, Docker Compose, and Helm. [PR #1](https://github.com/munozajj/caplog/pull/1)

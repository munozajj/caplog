"""The logbook database.

append_with is the only method that inserts rows. It takes a transaction
advisory lock, assigns the next entry number, and stores the hash. Triggers
in the database repeat those checks so a raw INSERT cannot skip the chain.
"""

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from caplog.accounts import (
    check_password,
    check_person_name,
    check_username,
    hash_password,
    new_token,
    verify_password,
)
from caplog.chain import (
    GENESIS_HASH,
    KIND_ENTRY,
    KIND_EXPORT,
    KIND_GENESIS,
    KIND_IMPORT,
    KIND_IMPORTED,
    KIND_SYSTEM,
    ChainChecker,
    ChainProblem,
    Record,
    check_chain,
    compute_hash,
)
from caplog.checkpoint import Checkpoint, read_all
from caplog.errors import ErrDenied, ErrLocked, ErrNoTitle, ErrRateLimited, ErrTitleIsSet, ValidationError
from caplog.jsonutil import RECORD_KEYS, dumps
from caplog.prefs import BOOL_KEYS, CHOICES, DEFAULTS, DISPLAY_KEYS, INT_LIMITS
from caplog.schema import STATEMENTS
from caplog.timeutil import format_time

log = logging.getLogger("caplog")

MAX_TEXT_CHARS = 500
MAX_NAME_CHARS = 100
MAX_TITLE_CHARS = 200
MAX_IP_CHARS = 64
EXPORT_FORMAT = "caplog-export"
EXPORT_VERSION = 1

_COLS = "seq, kind, created_at, first_name, last_name, ip, body, meta, prev_hash, hash"
_INSERT = (
    "INSERT INTO records (seq, kind, created_at, first_name, last_name, ip, body, meta, prev_hash, hash) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def migrate(dsn: str, app_role: str = "caplog_app") -> None:
    """Create the table and triggers, then grant the application role insert and select."""
    role = sql.Identifier(app_role)
    with psycopg.connect(dsn, autocommit=True) as conn:
        for statement in STATEMENTS:
            conn.execute(statement)
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        conn.execute(sql.SQL("REVOKE ALL ON TABLE records FROM {}").format(role))
        conn.execute(sql.SQL("REVOKE ALL ON TABLE records FROM PUBLIC"))
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE records TO {}").format(role))
        for table in ("users", "sessions", "settings", "login_attempts", "post_hits", "correction_links"):
            conn.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM PUBLIC").format(sql.Identifier(table)))
            conn.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(sql.Identifier(table), role))
        for table in ("users", "settings"):
            conn.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE {} TO {}").format(sql.Identifier(table), role)
            )
        for table in ("sessions", "login_attempts", "post_hits"):
            conn.execute(
                sql.SQL("GRANT SELECT, INSERT, DELETE ON TABLE {} TO {}").format(sql.Identifier(table), role)
            )
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE correction_links TO {}").format(role))
        for sequence in (
            "users_id_seq",
            "login_attempts_id_seq",
            "post_hits_id_seq",
        ):
            conn.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(sql.Identifier(sequence), role))


@dataclass
class ImportResult:
    source_title: str
    imported: int
    first_seq: int
    last_seq: int

    def to_dict(self) -> dict:
        return {
            "source_title": self.source_title,
            "imported": self.imported,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
        }


@dataclass
class VerifyReport:
    ok: bool
    title: str
    records: int
    head_seq: int
    head_hash: str
    chain_ok: bool
    chain_problem: ChainProblem | None
    checkpoints: dict
    problems: list[str]
    warnings: list[str]
    summary: str

    def to_dict(self) -> dict:
        data = {
            "ok": self.ok,
            "title": self.title,
            "records": self.records,
            "head_seq": self.head_seq,
            "head_hash": self.head_hash,
            "chain_ok": self.chain_ok,
            "checkpoints": self.checkpoints,
            "problems": self.problems,
            "warnings": self.warnings,
            "summary": self.summary,
        }
        if self.chain_problem is not None:
            data["chain_problem"] = self.chain_problem.to_dict()
        return data


class Store:
    def __init__(self, dsn: str, now: Callable[[], datetime] | None = None) -> None:
        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=4,
            open=True,
            kwargs={
                "row_factory": dict_row,
                "autocommit": False,
                "application_name": "caplog",
            },
        )
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._write_lock = threading.Lock()
        self._on_append: list[Callable[[list[Record]], None]] = []

    def close(self) -> None:
        self.pool.close()

    def on_append(self, fn: Callable[[list[Record]], None]) -> None:
        self._on_append.append(fn)

    def ping(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT 1")

    def title(self) -> str:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT body FROM records WHERE seq = 0").fetchone()
        if row is None:
            raise ErrNoTitle()
        return row["body"]

    def head(self) -> Record:
        with self.pool.connection() as conn:
            row = conn.execute(f"SELECT {_COLS} FROM records ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            raise ErrNoTitle()
        return Record.from_row(row)

    def get(self, seq: int) -> Record | None:
        with self.pool.connection() as conn:
            row = conn.execute(f"SELECT {_COLS} FROM records WHERE seq = %s", (seq,)).fetchone()
        return Record.from_row(row) if row else None

    def list_after(self, after: int, limit: int) -> list[Record]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLS} FROM records WHERE seq > %s ORDER BY seq LIMIT %s",
                (after, limit),
            ).fetchall()
        return [Record.from_row(row) for row in rows]

    def each(self, max_seq: int, fn: Callable[[Record], None]) -> None:
        if max_seq < 0:
            max_seq = 2**62 - 1
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLS} FROM records WHERE seq <= %s ORDER BY seq",
                (max_seq,),
            )
            for row in rows:
                fn(Record.from_row(row))

    def setup(self, title: str) -> Record:
        title = normalize_newlines(title).strip()
        if not title:
            raise ValidationError("a logbook title is required")
        if len(title) > MAX_TITLE_CHARS:
            raise ValidationError(f"the title can be at most {MAX_TITLE_CHARS} characters")
        return self._append(True, [Record(kind=KIND_GENESIS, body=title)])[0]

    def add_entry(
        self,
        first: str,
        last: str,
        text: str,
        ip: str,
        meta: str = "",
        side: Callable | None = None,
    ) -> Record:
        first = first.strip()
        last = last.strip()
        text = normalize_newlines(text).strip()
        ip = ip.strip()
        if not first or not last:
            raise ValidationError("first name and last name are both required")
        if "\n" in first or "\n" in last:
            raise ValidationError("names must be on one line")
        if len(first) > MAX_NAME_CHARS or len(last) > MAX_NAME_CHARS:
            raise ValidationError(f"names can be at most {MAX_NAME_CHARS} characters each")
        if not text:
            raise ValidationError("the entry text is required")
        limit = self.max_text()
        if len(text) > limit:
            raise ValidationError(f"the entry text can be at most {limit} characters")
        if "\n" in ip:
            raise ValidationError("ip must be on one line")
        if len(ip) > MAX_IP_CHARS:
            raise ValidationError(f"ip can be at most {MAX_IP_CHARS} characters")
        return self._append(
            False,
            [Record(kind="entry", first_name=first, last_name=last, body=text, ip=ip, meta=meta or "")],
            side,
        )[0]

    def record_export(self, fmt: str) -> Record:
        label = {"json": "JSON", "pdf": "PDF"}[fmt]

        def build(head: Record) -> list[Record]:
            to_seq = head.seq + 1
            return [
                Record(
                    kind=KIND_EXPORT,
                    body=f"Logbook exported as {label} (records #0-#{to_seq}).",
                    meta=dumps({"format": fmt, "from_seq": 0, "to_seq": to_seq}),
                )
            ]

        return self._append_with(False, build)[0]

    def write_json_export(self, handle, up_to: Record) -> None:
        title = self.title()
        header = dumps(
            {
                "format": EXPORT_FORMAT,
                "version": EXPORT_VERSION,
                "title": title,
                "exported_at": up_to.created_at,
            }
        )
        handle.write(header[:-1].encode())
        handle.write(b',\n"records":[\n')
        first = True

        def write_one(record: Record) -> None:
            nonlocal first
            blob = dumps(record.to_dict()).encode()
            if not first:
                handle.write(b",\n")
            first = False
            handle.write(blob)

        self.each(up_to.seq, write_one)
        handle.write(b"\n]}\n")

    def import_data(self, data: bytes) -> ImportResult:
        parsed = parse_export(data, self.max_text())
        source = parsed.records[1:]
        if not source:
            raise ValidationError("the export has a title but no entries to import")
        last = parsed.records[-1]
        count = len(source)

        def note(phase: str) -> str:
            return dumps(
                {
                    "phase": phase,
                    "source_title": parsed.title,
                    "source_last_hash": last.hash,
                    "count": count,
                }
            )

        quoted = json.dumps(parsed.title, ensure_ascii=False)
        drafts = [
            Record(
                kind=KIND_IMPORT,
                body=f"Import started: {count} records from logbook {quoted}.",
                meta=note("start"),
            )
        ]
        for record in source:
            meta = {
                "source_title": parsed.title,
                "source_seq": record.seq,
                "source_kind": record.kind,
                "source_created_at": record.created_at,
                "source_hash": record.hash,
            }
            if record.meta:
                meta["source_meta"] = record.meta
            drafts.append(
                Record(
                    kind=KIND_IMPORTED,
                    first_name=record.first_name,
                    last_name=record.last_name,
                    ip=record.ip,
                    body=record.body,
                    meta=dumps(meta),
                )
            )
        drafts.append(
            Record(
                kind=KIND_IMPORT,
                body=f"Import finished: {count} records from logbook {quoted}.",
                meta=note("end"),
            )
        )
        saved = self._append(False, drafts)
        return ImportResult(parsed.title, count, saved[0].seq, saved[-1].seq)

    def verify(self, checkpoint_path: str, public_key) -> VerifyReport:
        report = VerifyReport(
            ok=False,
            title="",
            records=0,
            head_seq=-1,
            head_hash="",
            chain_ok=False,
            chain_problem=None,
            checkpoints={
                "file": checkpoint_path or "",
                "total": 0,
                "matching": 0,
                "signatures_checked": public_key is not None,
                "latest_seq": -1,
                "latest_time": "",
            },
            problems=[],
            warnings=[],
            summary="",
        )
        checkpoints: list[Checkpoint] = []
        if checkpoint_path:
            try:
                checkpoints = read_all(checkpoint_path)
            except Exception as exc:
                report.problems.append(str(exc))
        wanted: dict[int, str] = {item.seq: "" for item in checkpoints}
        checker = ChainChecker()
        try:
            def visit(record: Record) -> None:
                if record.seq == 0:
                    report.title = record.body
                if record.seq in wanted:
                    wanted[record.seq] = record.hash
                report.head_seq = record.seq
                report.head_hash = record.hash
                if report.chain_problem is None:
                    report.chain_problem = checker.add(record)

            self.each(-1, visit)
        except Exception as exc:
            report.problems.append("could not read the database: " + str(exc))
        report.records = checker.count()
        report.chain_ok = report.chain_problem is None and not any(
            item.startswith("could not read") for item in report.problems
        )
        if report.chain_problem is not None:
            report.problems.append(
                f"hash chain broken at record #{report.chain_problem.seq}: {report.chain_problem.reason}"
            )
        report.checkpoints["total"] = len(checkpoints)
        my_id = ""
        if public_key is not None:
            from caplog.checkpoint import key_id

            my_id = key_id(public_key)
        other_key = 0
        for item in checkpoints:
            if item.seq > report.checkpoints["latest_seq"]:
                report.checkpoints["latest_seq"] = item.seq
                report.checkpoints["latest_time"] = item.time
            if public_key is not None:
                if item.key_id != my_id:
                    other_key += 1
                    continue
                if not item.verify_sig(public_key):
                    report.problems.append(
                        f"checkpoint for record #{item.seq} ({item.time}) has an invalid signature "
                        "(checkpoint file was edited)"
                    )
                    continue
            got = wanted.get(item.seq, "")
            if got == "":
                report.problems.append(
                    f"checkpoint from {item.time} covers record #{item.seq}, but the database only "
                    f"goes up to #{report.head_seq} (records were removed or the database was rolled back)"
                )
            elif got != item.hash:
                report.problems.append(
                    f"record #{item.seq} no longer matches its signed checkpoint from {item.time} "
                    "(the log was rewritten)"
                )
            else:
                report.checkpoints["matching"] += 1
        if other_key:
            report.warnings.append(
                f"{other_key} checkpoint(s) were signed by a different key and could not be checked"
            )
        if public_key is None:
            report.warnings.append(
                "no signing key or public key is configured, so checkpoint signatures were NOT "
                "checked; set CAPLOG_SIGNING_KEY (see README)"
            )
        if not checkpoint_path:
            report.warnings.append("no checkpoint file configured")
        elif not checkpoints and report.head_seq >= 0:
            report.warnings.append(
                "no signed checkpoints exist yet, so a fully rewritten log could not be detected"
            )
        if report.head_seq < 0:
            report.warnings.append("the logbook is empty (not set up yet)")
        report.ok = not report.problems
        if report.ok:
            report.summary = (
                f"OK: {report.records} records verified; "
                f"{report.checkpoints['matching']} of {report.checkpoints['total']} signed checkpoints match."
            )
        else:
            report.summary = "FAILED: " + report.problems[0]
        return report

    def settings(self) -> dict[str, str]:
        found = dict(DEFAULTS)
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        for row in rows:
            if row["key"] in found:
                found[row["key"]] = row["value"]
        return found

    def max_text(self) -> int:
        try:
            return int(self.settings()["max_entry_length"])
        except (KeyError, ValueError):
            return MAX_TEXT_CHARS

    def _int_setting(self, key: str) -> int:
        return int(self.settings()[key])

    def clean_setting(self, key: str, value: str) -> str:
        if key not in DEFAULTS:
            raise ValidationError("unknown setting")
        if key != "notice":
            value = str(value).strip()
        else:
            value = normalize_newlines(str(value)).strip()
        if key in BOOL_KEYS:
            if value not in ("true", "false"):
                raise ValidationError(f"{key} must be true or false")
            return value
        if key in CHOICES:
            if value not in CHOICES[key]:
                raise ValidationError(f"{key} is not an allowed value")
            return value
        if key in INT_LIMITS:
            low, high = INT_LIMITS[key]
            try:
                number = int(value)
            except ValueError as exc:
                raise ValidationError(f"{key} must be a number") from exc
            if not low <= number <= high:
                raise ValidationError(f"{key} must be from {low} to {high}")
            return str(number)
        if key == "accent_color":
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
                raise ValidationError("the accent color must be a color such as #0d6e6a")
            return value.lower()
        if key in ("site_fqdn", "ntp_server"):
            if value and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", value):
                raise ValidationError(f"{key} must be a host name")
            return value
        if key == "tags":
            parts: list[str] = []
            for item in value.split(","):
                item = item.strip()
                if not item:
                    continue
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 -]{0,39}", item):
                    raise ValidationError("a tag must start with a letter and be at most 40 characters")
                if item not in parts:
                    parts.append(item)
            return ",".join(parts)
        if key == "notice":
            if len(value) > 500:
                raise ValidationError("the notice can be at most 500 characters")
            return value
        if key == "export_last":
            return value[:40]
        raise ValidationError("unknown setting")

    def log_system(self, body: str, meta: dict | None = None) -> Record:
        record = Record(
            kind=KIND_SYSTEM,
            first_name="CapLog",
            last_name="System",
            body=body,
            meta=dumps(meta or {"event": "system"}),
        )
        return self._append(False, [record])[0]

    def set_setting(self, key: str, value: str) -> Record:
        value = self.clean_setting(key, value)
        if self.settings().get(key) == value:
            return self.head()
        if key == "notice":
            body = "Notice cleared." if not value else f"Notice set: {value}"
        else:
            body = f"Setting {key} changed to {value}."
        record = Record(
            kind=KIND_SYSTEM,
            first_name="CapLog",
            last_name="System",
            body=body,
            meta=dumps({"event": "setting", "key": key}),
        )

        def side(conn, _saved) -> None:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )

        return self._append(False, [record], side)[0]

    def user_count(self) -> int:
        with self.pool.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def list_users(self) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, username, first_name, last_name, role, enabled, is_master, display_json "
                "FROM users ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def user_by_name(self, username: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = %s", (username,)).fetchone()
        return dict(row) if row else None

    def user_by_id(self, user_id: int) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        return dict(row) if row else None

    def setup_with_admin(self, title: str, username: str, first: str, last: str, password: str) -> Record:
        username = check_username(username)
        rules = self.settings().get("guest_name_rules") == "true"
        first = check_person_name(first, "first name", rules)
        last = check_person_name(last, "last name", rules)
        password = check_password(password)
        password_hash = hash_password(password)
        has_title = True
        try:
            self.title()
        except ErrNoTitle:
            has_title = False
        if has_title and self.user_count():
            raise ErrTitleIsSet()
        drafts: list[Record] = []
        if not has_title:
            title = normalize_newlines(title).strip()
            if not title:
                raise ValidationError("a logbook title is required")
            if len(title) > MAX_TITLE_CHARS:
                raise ValidationError(f"the title can be at most {MAX_TITLE_CHARS} characters")
            drafts.append(Record(kind=KIND_GENESIS, body=title))
        drafts.append(
            Record(
                kind=KIND_SYSTEM,
                first_name="CapLog",
                last_name="System",
                body=f"Master admin account created for {username}.",
                meta=dumps({"event": "admin-created", "username": username}),
            )
        )

        def side(conn, _saved) -> None:
            if conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]:
                raise ValidationError("an admin account already exists")
            conn.execute(
                "INSERT INTO users (username, first_name, last_name, password_hash, role, enabled, is_master, created_at) "
                "VALUES (%s, %s, %s, %s, 'admin', true, true, %s)",
                (username, first, last, password_hash, format_time(self.now())),
            )

        return self._append(not has_title, drafts, side)[-1]

    def create_user(self, username: str, first: str, last: str, password: str, role: str) -> None:
        if role not in ("admin", "user"):
            raise ValidationError("the role must be admin or user")
        username = check_username(username)
        rules = True
        first = check_person_name(first, "first name", rules)
        last = check_person_name(last, "last name", rules)
        password = check_password(password)
        password_hash = hash_password(password)
        record = Record(
            kind=KIND_SYSTEM,
            first_name="CapLog",
            last_name="System",
            body=f"Account created for {username} with role {role}.",
            meta=dumps({"event": "user-created", "username": username, "role": role}),
        )

        def side(conn, _saved) -> None:
            conn.execute(
                "INSERT INTO users (username, first_name, last_name, password_hash, role, enabled, is_master, created_at) "
                "VALUES (%s, %s, %s, %s, %s, true, false, %s)",
                (username, first, last, password_hash, role, format_time(self.now())),
            )

        try:
            self._append(False, [record], side)
        except UniqueViolation as exc:
            raise ValidationError("that username is already in use") from exc

    def set_user_enabled(self, username: str, enabled: bool) -> None:
        user = self.user_by_name(username)
        if user is None:
            raise ValidationError("that account was not found")
        if user["is_master"] and not enabled:
            raise ValidationError("the first admin account cannot be disabled")
        state = "enabled" if enabled else "disabled"
        record = Record(
            kind=KIND_SYSTEM,
            first_name="CapLog",
            last_name="System",
            body=f"Account {username} was {state}.",
            meta=dumps({"event": "user-enabled", "username": username, "enabled": enabled}),
        )

        def side(conn, _saved) -> None:
            conn.execute("UPDATE users SET enabled = %s WHERE username = %s", (enabled, username))

        self._append(False, [record], side)

    def set_user_role(self, username: str, role: str) -> None:
        if role not in ("admin", "user"):
            raise ValidationError("the role must be admin or user")
        user = self.user_by_name(username)
        if user is None:
            raise ValidationError("that account was not found")
        if user["is_master"] and role != "admin":
            raise ValidationError("the first admin account cannot be changed to a normal user")
        record = Record(
            kind=KIND_SYSTEM,
            first_name="CapLog",
            last_name="System",
            body=f"Account {username} role set to {role}.",
            meta=dumps({"event": "user-role", "username": username, "role": role}),
        )

        def side(conn, _saved) -> None:
            conn.execute("UPDATE users SET role = %s WHERE username = %s", (role, username))

        self._append(False, [record], side)

    def set_user_password(self, username: str, password: str) -> None:
        if self.user_by_name(username) is None:
            raise ValidationError("that account was not found")
        password = check_password(password)
        password_hash = hash_password(password)
        record = Record(
            kind=KIND_SYSTEM,
            first_name="CapLog",
            last_name="System",
            body=f"Password reset for {username}.",
            meta=dumps({"event": "password-reset", "username": username}),
        )

        def side(conn, _saved) -> None:
            conn.execute("UPDATE users SET password_hash = %s WHERE username = %s", (password_hash, username))

        self._append(False, [record], side)

    def _attempt_cutoff(self) -> str:
        minutes = self._int_setting("login_lock_minutes")
        return format_time(self.now() - timedelta(minutes=minutes))

    def _fail_count(self, column: str, value: str) -> int:
        if column not in ("username", "ip"):
            raise ValidationError("bad lockout field")
        query = f"SELECT COUNT(*) AS n FROM login_attempts WHERE {column} = %s AND ok = false AND at > %s"
        with self.pool.connection() as conn:
            return int(conn.execute(query, (value, self._attempt_cutoff())).fetchone()["n"])

    def login(self, username: str, password: str, ip: str) -> tuple[dict, str]:
        username = username.strip()
        now = format_time(self.now())
        user = self.user_by_name(username) if username else None
        if user and user["locked_until"] and user["locked_until"] > now:
            raise ErrLocked()
        if self._fail_count("ip", ip) >= self._int_setting("login_max_failures"):
            raise ErrLocked()
        ok = bool(user and user["enabled"] and verify_password(user["password_hash"], password))
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO login_attempts (username, ip, at, ok) VALUES (%s, %s, %s, %s)",
                (username, ip, now, ok),
            )
        if not ok:
            if user and self._fail_count("username", username) >= self._int_setting("login_max_failures"):
                until = format_time(self.now() + timedelta(minutes=self._int_setting("login_lock_minutes")))
                with self.pool.connection() as conn:
                    conn.execute("UPDATE users SET locked_until = %s WHERE username = %s", (until, username))
                raise ErrLocked()
            if self._fail_count("ip", ip) >= self._int_setting("login_max_failures"):
                raise ErrLocked()
            raise ErrDenied("the username or password is not correct")
        token = new_token()
        expires = format_time(self.now() + timedelta(hours=12))
        with self.pool.connection() as conn:
            conn.execute("UPDATE users SET locked_until = '' WHERE id = %s", (user["id"],))
            conn.execute("DELETE FROM login_attempts WHERE username = %s", (username,))
            conn.execute(
                "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, %s)",
                (token, user["id"], expires),
            )
        return user, token

    def logout(self, token: str) -> None:
        if not token:
            return
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM sessions WHERE token = %s", (token,))

    def session_user(self, token: str) -> dict | None:
        if not token:
            return None
        now = format_time(self.now())
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token = %s AND s.expires_at > %s",
                (token, now),
            ).fetchone()
        if row is None or not row["enabled"]:
            return None
        return dict(row)

    def save_display(self, user_id: int, data: dict) -> None:
        clean = {key: self.clean_setting(key, data[key]) for key in DISPLAY_KEYS if key in data}
        with self.pool.connection() as conn:
            conn.execute("UPDATE users SET display_json = %s WHERE id = %s", (dumps(clean), user_id))

    def check_post_rate(self, ip: str, user_id: int | None) -> None:
        if self.settings().get("feature_rate_limit") != "true":
            return
        limit = self._int_setting("rate_limit_count")
        window = self._int_setting("rate_limit_window")
        cutoff = format_time(self.now() - timedelta(seconds=window))
        buckets = [f"ip:{ip}"]
        if user_id:
            buckets.append(f"user:{user_id}")
        with self.pool.connection() as conn:
            for bucket in buckets:
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM post_hits WHERE bucket = %s AND at > %s",
                    (bucket, cutoff),
                ).fetchone()["n"]
                if count >= limit:
                    raise ErrRateLimited()

    def record_hit(self, ip: str, user_id: int | None) -> None:
        if self.settings().get("feature_rate_limit") != "true":
            return
        now = format_time(self.now())
        buckets = [f"ip:{ip}"]
        if user_id:
            buckets.append(f"user:{user_id}")
        with self.pool.connection() as conn:
            for bucket in buckets:
                conn.execute("INSERT INTO post_hits (bucket, at) VALUES (%s, %s)", (bucket, now))

    def correction_map(self) -> dict[int, list[int]]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT new_seq, old_seq FROM correction_links").fetchall()
        found: dict[int, list[int]] = {}
        for row in rows:
            found.setdefault(int(row["old_seq"]), []).append(int(row["new_seq"]))
        return found

    def search_records(self, q: str, name: str, start: str, end: str, tag: str, limit: int) -> list[Record]:
        if self.settings().get("feature_search") != "true" and any((q, name, start, end, tag)):
            raise ErrDenied("search is turned off")
        clauses = ["TRUE"]
        params: list = []
        if q:
            like = "%" + q.replace("%", "").replace("_", "") + "%"
            clauses.append("(body ILIKE %s OR first_name ILIKE %s OR last_name ILIKE %s)")
            params.extend((like, like, like))
        if name:
            like = "%" + name.replace("%", "").replace("_", "") + "%"
            clauses.append("(first_name ILIKE %s OR last_name ILIKE %s)")
            params.extend((like, like))
        if start:
            clauses.append("created_at >= %s")
            params.append(start)
        if end:
            clauses.append("created_at <= %s")
            params.append(end)
        if tag:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 -]{0,39}", tag):
                raise ValidationError("that tag is not valid")
            clauses.append("meta LIKE %s")
            params.append('%"tag":"' + tag + '"%')
        params.append(limit)
        query = f"SELECT {_COLS} FROM records WHERE {' AND '.join(clauses)} ORDER BY seq DESC LIMIT %s"
        with self.pool.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [Record.from_row(row) for row in rows]

    def _append(self, genesis: bool, drafts: list[Record], side: Callable | None = None) -> list[Record]:
        return self._append_with(genesis, lambda _head: drafts, side)

    def _append_with(
        self,
        genesis: bool,
        build: Callable[[Record], list[Record]],
        side: Callable | None = None,
    ) -> list[Record]:
        with self._write_lock:
            with self.pool.connection() as conn:
                with conn.transaction():
                    # Same lock the insert trigger takes, so the head cannot change
                    # between the read below and the insert.
                    conn.execute("SELECT pg_advisory_xact_lock(847291)")
                    row = conn.execute(
                        f"SELECT {_COLS} FROM records ORDER BY seq DESC LIMIT 1"
                    ).fetchone()
                    if row is None:
                        head = Record(seq=-1, hash=GENESIS_HASH)
                    else:
                        head = Record.from_row(row)
                    if genesis and head.seq >= 0:
                        raise ErrTitleIsSet()
                    if not genesis and head.seq < 0:
                        raise ErrNoTitle()
                    drafts = build(head)
                    stamp = format_time(self.now())
                    seq = head.seq
                    prev = head.hash
                    saved: list[Record] = []
                    for record in drafts:
                        seq += 1
                        record.seq = seq
                        record.created_at = stamp
                        record.prev_hash = prev
                        record.first_name = record.first_name or ""
                        record.last_name = record.last_name or ""
                        record.ip = record.ip or ""
                        record.body = record.body or ""
                        record.meta = record.meta or ""
                        record.hash = compute_hash(record)
                        conn.execute(
                            _INSERT,
                            (
                                record.seq,
                                record.kind,
                                record.created_at,
                                record.first_name,
                                record.last_name,
                                record.ip,
                                record.body,
                                record.meta,
                                record.prev_hash,
                                record.hash,
                            ),
                        )
                        prev = record.hash
                        saved.append(record)
                    if side is not None:
                        side(conn, saved)
            for fn in self._on_append:
                try:
                    fn(saved)
                except Exception:
                    # The rows are already committed. A live-update failure
                    # must not turn that into an error for the caller.
                    log.exception("append notification failed")
            return saved


@dataclass
class ExportFile:
    title: str
    exported_at: str
    records: list[Record]


def _require_str(data: dict, key: str, seq: int) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValidationError(f"record #{seq} has fields that are too long or not valid text")
    return value


def parse_export(data: bytes, max_text: int = MAX_TEXT_CHARS) -> ExportFile:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"not a valid CapLog JSON export: {exc}") from exc
    if not isinstance(obj, dict) or set(obj) != {"format", "version", "title", "exported_at", "records"}:
        if not isinstance(obj, dict):
            raise ValidationError("not a valid CapLog JSON export: expected an object")
        extra = set(obj) - {"format", "version", "title", "exported_at", "records"}
        missing = {"format", "version", "title", "exported_at", "records"} - set(obj)
        if extra or missing:
            raise ValidationError("not a valid CapLog JSON export: unexpected fields")
    if obj.get("format") != EXPORT_FORMAT or type(obj.get("version")) is not int or obj["version"] != 1:
        raise ValidationError(
            f"not a CapLog export (format {obj.get('format')!r} version {obj.get('version')})"
        )
    if not isinstance(obj.get("title"), str) or not isinstance(obj.get("exported_at"), str):
        raise ValidationError("not a valid CapLog JSON export: bad title or time")
    raw_records = obj["records"]
    if not isinstance(raw_records, list) or not raw_records:
        raise ValidationError("the export contains no records")
    records: list[Record] = []
    for raw in raw_records:
        if not isinstance(raw, dict) or set(raw) != set(RECORD_KEYS):
            raise ValidationError("not a valid CapLog JSON export: unexpected record fields")
        if type(raw["seq"]) is not int:
            raise ValidationError("not a valid CapLog JSON export: bad entry number")
        seq = raw["seq"]
        fields = {key: _require_str(raw, key, seq) for key in RECORD_KEYS if key != "seq"}
        record = Record(seq=seq, **fields)
        if (
            len(fields["first_name"]) > MAX_NAME_CHARS
            or len(fields["last_name"]) > MAX_NAME_CHARS
            or len(fields["ip"]) > MAX_IP_CHARS
            or len(fields["body"]) > 2000
            or len(fields["meta"].encode("utf-8")) > 64 * 1024
        ):
            raise ValidationError(f"record #{seq} has fields that are too long or not valid text")
        limit = max(max_text, MAX_TEXT_CHARS)
        if record.kind in (KIND_ENTRY, KIND_IMPORTED) and record.first_name and len(record.body) > limit:
            raise ValidationError(f"record #{seq} is longer than {limit} characters")
        records.append(record)
    problem = check_chain(records)
    if problem is not None:
        raise ValidationError(f"hash chain is broken at record #{problem.seq}: {problem.reason}")
    if records[0].body != obj["title"]:
        raise ValidationError("the title in the file does not match its title record")
    return ExportFile(title=obj["title"], exported_at=obj["exported_at"], records=records)

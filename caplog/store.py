"""The logbook database.

append_with is the only method that inserts rows. It takes a transaction
advisory lock, assigns the next entry number, and stores the hash. Triggers
in the database repeat those checks so a raw INSERT cannot skip the chain.
"""

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from caplog.chain import (
    GENESIS_HASH,
    KIND_ENTRY,
    KIND_EXPORT,
    KIND_GENESIS,
    KIND_IMPORT,
    KIND_IMPORTED,
    ChainChecker,
    ChainProblem,
    Record,
    check_chain,
    compute_hash,
)
from caplog.checkpoint import Checkpoint, read_all
from caplog.errors import ErrNoTitle, ErrTitleIsSet, ValidationError
from caplog.jsonutil import RECORD_KEYS, dumps
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

    def add_entry(self, first: str, last: str, text: str, ip: str) -> Record:
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
        if len(text) > MAX_TEXT_CHARS:
            raise ValidationError(f"the entry text can be at most {MAX_TEXT_CHARS} characters")
        if "\n" in ip:
            raise ValidationError("ip must be on one line")
        if len(ip) > MAX_IP_CHARS:
            raise ValidationError(f"ip can be at most {MAX_IP_CHARS} characters")
        return self._append(
            False,
            [Record(kind="entry", first_name=first, last_name=last, body=text, ip=ip)],
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
        parsed = parse_export(data)
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

    def _append(self, genesis: bool, drafts: list[Record]) -> list[Record]:
        return self._append_with(genesis, lambda _head: drafts)

    def _append_with(self, genesis: bool, build: Callable[[Record], list[Record]]) -> list[Record]:
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


def parse_export(data: bytes) -> ExportFile:
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
        if record.kind in (KIND_ENTRY, KIND_IMPORTED) and record.first_name and len(record.body) > MAX_TEXT_CHARS:
            raise ValidationError(f"record #{seq} is longer than {MAX_TEXT_CHARS} characters")
        records.append(record)
    problem = check_chain(records)
    if problem is not None:
        raise ValidationError(f"hash chain is broken at record #{problem.seq}: {problem.reason}")
    if records[0].body != obj["title"]:
        raise ValidationError("the title in the file does not match its title record")
    return ExportFile(title=obj["title"], exported_at=obj["exported_at"], records=records)

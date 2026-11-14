import io

import psycopg
import pytest

from caplog.checkpoint import Writer, generate_key, load_public_key
from caplog.errors import ErrTitleIsSet, ValidationError
from caplog.store import parse_export


def test_title_once_and_text_limit(db):
    store = db["store"]
    title = store.setup("Watch Log")
    assert title.seq == 0
    assert title.ip == ""
    with pytest.raises(ErrTitleIsSet):
        store.setup("Other")
    ok = store.add_entry("Ada", "Lovelace", "é" * 500, "192.0.2.10")
    assert ok.seq == 1
    assert len(ok.body) == 500
    with pytest.raises(ValidationError, match="500"):
        store.add_entry("Ada", "Lovelace", "x" * 501, "192.0.2.10")


def test_triggers_and_permissions(db):
    store = db["store"]
    store.setup("Watch Log")
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="cannot be modified"):
            conn.execute("UPDATE records SET body = 'x' WHERE seq = 0")
        with pytest.raises(psycopg.Error, match="cannot be deleted"):
            conn.execute("DELETE FROM records WHERE seq = 0")
        with pytest.raises(psycopg.Error, match="cannot be truncated"):
            conn.execute("TRUNCATE records")
    with psycopg.connect(db["app"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE records SET body = 'x' WHERE seq = 0")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM records WHERE seq = 0")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("TRUNCATE records")


def test_verify_detects_tamper_and_rewritten_chain(db, tmp_path):
    store = db["store"]
    store.setup("Watch Log")
    store.add_entry("Ada", "Lovelace", "one", "192.0.2.10")
    store.add_entry("Grace", "Hopper", "two", "192.0.2.11")
    assert store.verify("", None).ok
    private, public = generate_key()
    key = tmp_path / "k.pem"
    pub = tmp_path / "k.pub"
    key.write_bytes(private)
    pub.write_bytes(public)
    writer = Writer(str(tmp_path / "cp.jsonl"), __import__("caplog.checkpoint", fromlist=["load_private_key"]).load_private_key(key))
    head = store.head()
    writer.write(head.seq, head.hash)
    # Rewrite record 1 and recompute the rest of the chain so the links look valid.
    from caplog.chain import Record, compute_hash

    with psycopg.connect(db["admin"], autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        row = conn.execute(
            "SELECT seq, kind, created_at, first_name, last_name, ip, body, meta, prev_hash, hash "
            "FROM records WHERE seq = 1"
        ).fetchone()
        record = Record(
            seq=row[0],
            kind=row[1],
            created_at=row[2],
            first_name=row[3],
            last_name=row[4],
            ip=row[5],
            body="rewritten",
            meta=row[7],
            prev_hash=row[8],
        )
        record.hash = compute_hash(record)
        conn.execute(
            "UPDATE records SET body = %s, hash = %s WHERE seq = 1",
            (record.body, record.hash),
        )
        nxt = conn.execute(
            "SELECT seq, kind, created_at, first_name, last_name, ip, body, meta FROM records WHERE seq = 2"
        ).fetchone()
        follow = Record(
            seq=nxt[0],
            kind=nxt[1],
            created_at=nxt[2],
            first_name=nxt[3],
            last_name=nxt[4],
            ip=nxt[5],
            body=nxt[6],
            meta=nxt[7],
            prev_hash=record.hash,
        )
        follow.hash = compute_hash(follow)
        conn.execute(
            "UPDATE records SET prev_hash = %s, hash = %s WHERE seq = 2",
            (follow.prev_hash, follow.hash),
        )
        conn.execute("SET session_replication_role = DEFAULT")
    report = store.verify(str(tmp_path / "cp.jsonl"), load_public_key(pub))
    assert report.chain_ok
    assert not report.ok
    assert any("rewritten" in problem for problem in report.problems)


def test_import_export_round_trip(db):
    store = db["store"]
    store.setup("Watch Log")
    store.add_entry("Ada", "Lovelace", "café", "192.0.2.9")
    exported = store.record_export("json")
    assert exported.ip == ""
    buf = io.BytesIO()
    store.write_json_export(buf, exported)
    parsed = parse_export(buf.getvalue())
    assert parsed.title == "Watch Log"
    result = store.import_data(buf.getvalue())
    assert result.imported == len(parsed.records) - 1
    assert "Import started" in store.get(result.first_seq).body
    assert store.verify("", None).ok
    # A changed export is rejected and adds nothing.
    before = store.head().seq
    broken = bytearray(buf.getvalue().replace(b"caf\xc3\xa9", b"xxxx"))
    with pytest.raises(Exception):
        store.import_data(bytes(broken))
    assert store.head().seq == before

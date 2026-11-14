import hashlib

from caplog.chain import GENESIS_HASH, KIND_GENESIS, ChainChecker, Record, compute_hash


def test_hash_matches_documented_encoding():
    record = Record(
        seq=0,
        kind=KIND_GENESIS,
        created_at="2026-01-01T00:00:00Z",
        body="Watch Log",
        prev_hash=GENESIS_HASH,
    )
    canon = (
        "caplog-v1\n"
        "1:0\n"
        "7:genesis\n"
        "20:2026-01-01T00:00:00Z\n"
        "0:\n"
        "0:\n"
        "0:\n"
        "9:Watch Log\n"
        "0:\n"
        f"64:{GENESIS_HASH}\n"
    )
    assert compute_hash(record) == hashlib.sha256(canon.encode()).hexdigest()


def test_hash_uses_utf8_byte_length():
    record = Record(seq=1, kind="entry", created_at="2026-01-01T00:00:00Z", body="café", prev_hash="ab")
    text = compute_hash(record)
    # "café" is 5 bytes. The field line starts with "5:".
    raw = (
        "caplog-v1\n"
        "1:1\n"
        "5:entry\n"
        "20:2026-01-01T00:00:00Z\n"
        "0:\n"
        "0:\n"
        "0:\n"
        "5:café\n"
        "0:\n"
        "2:ab\n"
    )
    assert "5:café\n".encode() == b"5:caf\xc3\xa9\n"
    assert text == hashlib.sha256(raw.encode()).hexdigest()


def test_tamper_is_the_first_bad_record():
    first = Record(
        seq=0,
        kind=KIND_GENESIS,
        created_at="2026-01-01T00:00:00Z",
        body="Title",
        prev_hash=GENESIS_HASH,
    )
    first.hash = compute_hash(first)
    second = Record(
        seq=1,
        kind="entry",
        created_at="2026-01-01T00:00:01Z",
        first_name="Ada",
        last_name="Lovelace",
        body="hello",
        prev_hash=first.hash,
    )
    second.hash = compute_hash(second)
    second.body = "changed"
    checker = ChainChecker()
    assert checker.add(first) is None
    problem = checker.add(second)
    assert problem is not None
    assert problem.seq == 1
    assert "altered" in problem.reason

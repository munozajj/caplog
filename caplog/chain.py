"""Hash chain.

Each record stores the SHA-256 of a fixed text block. The block is the line
"caplog-v1" followed by every field, in order, written as "<byte length>:<value>".
The length is the number of UTF-8 bytes, not the number of characters. Two
different records cannot produce the same block by moving bytes between fields.

Field order: seq, kind, created_at, first_name, last_name, ip, body, meta, prev_hash.
The hash field itself is not included. prev_hash of record 0 is 64 zeros.
"""

import hashlib
from dataclasses import dataclass

from caplog.jsonutil import RECORD_KEYS

GENESIS_HASH = "0" * 64

KIND_GENESIS = "genesis"
KIND_ENTRY = "entry"
KIND_EXPORT = "export"
KIND_IMPORT = "import"
KIND_IMPORTED = "imported"


@dataclass
class Record:
    seq: int = 0
    kind: str = ""
    created_at: str = ""
    first_name: str = ""
    last_name: str = ""
    ip: str = ""
    body: str = ""
    meta: str = ""
    prev_hash: str = ""
    hash: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "created_at": self.created_at,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "ip": self.ip,
            "body": self.body,
            "meta": self.meta,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        return cls(**{key: data[key] for key in RECORD_KEYS})

    @classmethod
    def from_row(cls, row: dict) -> "Record":
        return cls(
            seq=row["seq"],
            kind=row["kind"],
            created_at=row["created_at"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            ip=row["ip"],
            body=row["body"],
            meta=row["meta"],
            prev_hash=row["prev_hash"],
            hash=row["hash"],
        )


def _field(value: str) -> str:
    raw = value.encode("utf-8")
    return f"{len(raw)}:{value}\n"


def compute_hash(record: Record) -> str:
    parts = (
        str(record.seq),
        record.kind,
        record.created_at,
        record.first_name,
        record.last_name,
        record.ip,
        record.body,
        record.meta,
        record.prev_hash,
    )
    text = "caplog-v1\n" + "".join(_field(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ChainProblem:
    seq: int
    reason: str

    def to_dict(self) -> dict:
        return {"seq": self.seq, "reason": self.reason}


class ChainChecker:
    """Check records one at a time, starting at #0."""

    def __init__(self) -> None:
        self._next = 0
        self._prev = GENESIS_HASH
        self.last: Record | None = None

    def add(self, record: Record) -> ChainProblem | None:
        want = self._next
        if record.seq != want:
            return ChainProblem(
                want,
                f"entry number {record.seq} found where {want} was expected "
                "(entries missing or added)",
            )
        if want == 0 and record.kind != KIND_GENESIS:
            return ChainProblem(0, "record #0 is not the title (genesis) record")
        if want > 0 and record.kind == KIND_GENESIS:
            return ChainProblem(record.seq, "a second title record appears")
        if record.prev_hash != self._prev:
            return ChainProblem(record.seq, "previous-hash link does not match the record before it")
        if compute_hash(record) != record.hash:
            return ChainProblem(record.seq, "content does not match its fingerprint (record was altered)")
        self._next += 1
        self._prev = record.hash
        self.last = record
        return None

    def count(self) -> int:
        return self._next


def check_chain(records: list[Record]) -> ChainProblem | None:
    checker = ChainChecker()
    for record in records:
        problem = checker.add(record)
        if problem is not None:
            return problem
    return None

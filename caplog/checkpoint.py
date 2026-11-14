"""Signed checkpoints.

A checkpoint says: at this time, the newest record was #N and its fingerprint
was H. It is signed with an Ed25519 private key that is never stored in the
database. The signed text is exactly:

    caplog-checkpoint-v1
    <seq>
    <hash>
    <time>

The checkpoint file is JSON lines: {seq, hash, time, key_id, sig}.
"""

import base64
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from caplog.timeutil import format_time

log = logging.getLogger("caplog")


def message(seq: int, digest: str, when: str) -> bytes:
    return f"caplog-checkpoint-v1\n{seq}\n{digest}\n{when}\n".encode()


def key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def generate_key() -> tuple[bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    priv_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, pub_pem


def load_private_key(path: str) -> Ed25519PrivateKey:
    data = open(path, "rb").read()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path}: not an Ed25519 key")
    return key


def load_public_key(path: str) -> Ed25519PublicKey:
    data = open(path, "rb").read()
    if b"PRIVATE KEY" in data.splitlines()[0]:
        return load_private_key(path).public_key()
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"{path}: not an Ed25519 key")
    return key


@dataclass
class Checkpoint:
    seq: int
    hash: str
    time: str
    key_id: str
    sig: str

    def verify_sig(self, public_key: Ed25519PublicKey) -> bool:
        try:
            sig = base64.standard_b64decode(self.sig)
            public_key.verify(sig, message(self.seq, self.hash, self.time))
            return True
        except Exception:
            return False

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "hash": self.hash,
            "time": self.time,
            "key_id": self.key_id,
            "sig": self.sig,
        }


def read_all(path: str) -> list[Checkpoint]:
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, "rb") as handle:
        for line_no, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
                out.append(
                    Checkpoint(
                        seq=data["seq"],
                        hash=data["hash"],
                        time=data["time"],
                        key_id=data["key_id"],
                        sig=data["sig"],
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError(f"checkpoint file line {line_no} is damaged: {exc}") from exc
    return out


class Writer:
    def __init__(self, path: str, private_key: Ed25519PrivateKey) -> None:
        self.path = path
        self.private_key = private_key
        self._lock = threading.Lock()
        existing = read_all(path)
        self.last = existing[-1].seq if existing else -1

    def write(self, seq: int, digest: str, now: datetime | None = None) -> Checkpoint | None:
        with self._lock:
            if seq <= self.last:
                return None
            when = format_time(now or datetime.now(timezone.utc))
            public = self.private_key.public_key()
            sig = base64.standard_b64encode(
                self.private_key.sign(message(seq, digest, when))
            ).decode("ascii")
            item = Checkpoint(seq=seq, hash=digest, time=when, key_id=key_id(public), sig=sig)
            line = json.dumps(item.to_dict(), separators=(",", ":")).encode() + b"\n"
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            self.last = seq
            return item


class Scheduler:
    """Checkpoint at startup, every N records, on a timer, and at shutdown."""

    def __init__(self, writer: Writer, head, every: int, interval: float) -> None:
        self.writer = writer
        self.head = head
        self.every = every
        self.interval = interval if interval > 0 else 600
        self._pending = 0
        self._lock = threading.Lock()
        self._kick = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="caplog-checkpoint", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def added(self, count: int) -> None:
        with self._lock:
            self._pending += count
            pending = self._pending
        if self.every > 0 and pending >= self.every:
            self._kick.set()

    def shutdown(self) -> None:
        self._stopped.set()
        self._kick.set()
        self._thread.join()

    def _run(self) -> None:
        self._checkpoint()
        while not self._stopped.is_set():
            self._kick.wait(self.interval)
            self._kick.clear()
            if self._stopped.is_set():
                break
            self._checkpoint()
        self._checkpoint()

    def _checkpoint(self) -> None:
        with self._lock:
            self._pending = 0
        try:
            seq, digest = self.head()
        except Exception:
            return
        try:
            item = self.writer.write(seq, digest)
        except Exception:
            log.exception("could not write signed checkpoint")
            return
        if item is not None:
            log.info("signed checkpoint written seq=%s", item.seq)

"""Start a local PostgreSQL for tests.

The process runs inside a user namespace so it is not uid 0. PostgreSQL
refuses to run as root, and this sandbox cannot create a normal user.
"""

import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

PG_BIN = Path(os.environ.get("CAPLOG_PG_BIN", "/tmp/pg/bin"))
LIB = str(PG_BIN.parent / "lib")

_WRAPPER = textwrap.dedent(
    """
    import ctypes, os, sys
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.syscall(272, 0x10000000) != 0:
        raise SystemExit("unshare failed")
    open("/proc/self/setgroups", "w").write("deny")
    open("/proc/self/uid_map", "w").write("1000 0 1\\n")
    open("/proc/self/gid_map", "w").write("1000 0 1\\n")
    os.execv(sys.argv[1], sys.argv[1:])
    """
)


def _popen(argv: list[str], **kwargs):
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = LIB
    env["LC_ALL"] = "C"
    env.pop("PGHOST", None)
    env.pop("PGPORT", None)
    return subprocess.Popen(
        [sys.executable, "-c", _WRAPPER, *argv],
        env=env,
        **kwargs,
    )


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Postgres:
    def __init__(self) -> None:
        self.data = Path(f"/tmp/caplog-pg-{os.getpid()}")
        self.port = _free_port()
        self.proc = None
        if self.data.exists():
            subprocess.run(["rm", "-rf", str(self.data)], check=True)
        self.data.mkdir()
        init = _popen(
            [
                str(PG_BIN / "initdb"),
                "-D",
                str(self.data),
                "--username=postgres",
                "--auth-local=trust",
                "--auth-host=trust",
                "--encoding=UTF8",
                "--locale=C",
                "--no-instructions",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        out, _ = init.communicate(timeout=60)
        if init.returncode != 0:
            raise RuntimeError(out.decode(errors="replace"))
        self.proc = _popen(
            [
                str(PG_BIN / "postgres"),
                "-D",
                str(self.data),
                "-p",
                str(self.port),
                "-h",
                "127.0.0.1",
                "-c",
                "listen_addresses=127.0.0.1",
                "-c",
                "unix_socket_directories=",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.url = f"postgresql://postgres@127.0.0.1:{self.port}/postgres"
        deadline = time.time() + 20
        import psycopg

        last = ""
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode(errors="replace")
                raise RuntimeError(err or "postgres exited")
            try:
                with psycopg.connect(self.url, connect_timeout=1) as conn:
                    conn.execute("SELECT 1")
                return
            except Exception as exc:
                last = str(exc)
                time.sleep(0.2)
        raise RuntimeError("postgres did not start: " + last)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        subprocess.run(["rm", "-rf", str(self.data)], check=False)

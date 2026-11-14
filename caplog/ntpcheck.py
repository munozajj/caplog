"""Compare the clock to an NTP server.

This asks the server what time it is. It does not set the clock.
"""

import socket
import struct
import time
from datetime import datetime, timezone

_NTP_EPOCH = 2208988800


def offset_seconds(server: str, timeout: float = 2.0) -> float:
    """Return local time minus the server time, in seconds."""
    host = server.strip()
    if not host or len(host) > 253 or any(ch.isspace() for ch in host):
        raise OSError("NTP server name is empty")
    packet = b"\x1b" + 47 * b"\0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(packet, (host, 123))
        data, _addr = sock.recvfrom(48)
    finally:
        sock.close()
    if len(data) < 48:
        raise OSError("NTP reply was too short")
    unpacked = struct.unpack("!12I", data)
    seconds = unpacked[10] - _NTP_EPOCH
    fraction = unpacked[11] / 2**32
    server_time = seconds + fraction
    return time.time() - server_time


def warning(server: str, threshold: float) -> str:
    if not server.strip():
        return ""
    try:
        drift = offset_seconds(server)
    except OSError as exc:
        return f"The time check could not reach {server.strip()}: {exc}"
    if abs(drift) > threshold:
        return (
            f"The clock is {abs(drift):.1f} seconds away from {server.strip()}. "
            "CapLog does not change the clock. Correct the time on the host."
        )
    return ""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

"""UTC timestamps in the same shape as Go's time.RFC3339Nano.

Fractional seconds are included only when they are not zero, and trailing
zeros are removed. The zone is always Z. Example: 2026-01-01T00:00:00Z
or 2026-01-01T00:00:00.123456Z.
"""

from datetime import datetime, timezone

_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}


def format_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        text += "." + f"{dt.microsecond:06d}".rstrip("0")
    return text + "Z"


def parse_duration(text: str) -> float:
    """Parse a Go-style duration such as 10m, 1h30m, or 45s. Returns seconds."""
    if not text:
        raise ValueError("empty duration")
    total = 0.0
    i = 0
    n = len(text)
    saw = False
    while i < n:
        start = i
        while i < n and (text[i].isdigit() or text[i] == "."):
            i += 1
        if i == start:
            raise ValueError(f"bad duration {text!r}")
        num = float(text[start:i])
        u_start = i
        while i < n and (text[i].isalpha() or text[i] == "µ"):
            i += 1
        unit = text[u_start:i]
        if unit not in _UNITS:
            raise ValueError(f"bad duration {text!r}")
        total += num * _UNITS[unit]
        saw = True
    if not saw:
        raise ValueError(f"bad duration {text!r}")
    return total

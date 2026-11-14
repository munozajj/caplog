"""JSON helpers.

Go's encoding/json escapes &, < and >. CapLog uses the same escaping so an
export from this program matches an export from the Go version field for field.
"""

import json

RECORD_KEYS = (
    "seq",
    "kind",
    "created_at",
    "first_name",
    "last_name",
    "ip",
    "body",
    "meta",
    "prev_hash",
    "hash",
)


def dumps(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("&", r"\u0026")
        .replace("<", r"\u003c")
        .replace(">", r"\u003e")
        .replace("\u2028", r"\u2028")
        .replace("\u2029", r"\u2029")
    )

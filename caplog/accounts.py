"""Local accounts. No external identity provider.

Passwords are hashed with argon2id. The hash stays in the users table.
It is never written into a log entry.
"""

import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from caplog.errors import ValidationError

_HASHER = PasswordHasher()
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{2,31}$")


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        ok = _HASHER.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return bool(ok)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def check_username(username: str) -> str:
    username = username.strip()
    if not _USERNAME.fullmatch(username):
        raise ValidationError(
            "a username must start with a letter and use 3 to 32 letters, digits, or hyphens"
        )
    return username


def check_password(password: str) -> str:
    if len(password) < 12 or len(password) > 200:
        raise ValidationError("a password must be 12 to 200 characters")
    if password.strip() != password or "\n" in password:
        raise ValidationError("a password must be on one line and cannot start or end with a space")
    return password


def check_person_name(value: str, label: str, rules: bool) -> str:
    value = value.strip()
    if not value or "\n" in value:
        raise ValidationError(f"{label} is required and must be on one line")
    limit = 60 if rules else 100
    if len(value) > limit:
        raise ValidationError(f"{label} can be at most {limit} characters")
    if rules:
        ok = value[0].isalpha() and all(ch.isalpha() or ch in " '-" for ch in value)
        if not ok:
            raise ValidationError(
                f"{label} may contain only letters, spaces, apostrophes, and hyphens"
            )
    return value

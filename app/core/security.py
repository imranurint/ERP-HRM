"""Password hashing and JWT handling.

* Passwords: Argon2id (`argon2-cffi`) — never stored or logged in the clear.
* Access tokens: short-lived signed JWTs carrying the user id and permissions.
* Refresh tokens: opaque high-entropy strings. Only a SHA-256 *hash* is
  persisted, so a database leak cannot be replayed. Rotation on every refresh
  plus explicit revocation gives us logout and breach containment.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings
from app.core.exceptions import UnauthorizedError
from app.core.ids import uuid7

TokenType = Literal["access", "refresh"]

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:  # pragma: no cover - corrupt hash, force a rehash
        return True


# --------------------------------------------------------------------------
# Access tokens (JWT)
# --------------------------------------------------------------------------
def create_access_token(
    *,
    user_id: UUID,
    permissions: list[str],
    is_superuser: bool = False,
    employee_id: UUID | None = None,
    expires_delta: timedelta | None = None,
) -> tuple[str, datetime]:
    """Return `(token, expires_at)`.

    `employee_id` is carried so self-service routes ("my leave", "my
    payslips") can scope without a second lookup. It is a convenience only —
    authorisation still re-reads permissions from the database.
    """
    now = datetime.now(UTC)
    expires_at = now + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": "access",
        "perms": permissions,
        "sup": is_superuser,
        "emp": str(employee_id) if employee_id else None,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": str(uuid7()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Access token has expired.", code="TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid access token.", code="INVALID_TOKEN") from exc

    if payload.get("type") != "access":
        raise UnauthorizedError("Wrong token type.", code="INVALID_TOKEN")
    return payload


# --------------------------------------------------------------------------
# Refresh tokens (opaque + hashed at rest)
# --------------------------------------------------------------------------
def generate_refresh_token() -> tuple[str, str]:
    """Return `(raw_token, token_hash)`. Only the hash is ever persisted."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def refresh_token_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)

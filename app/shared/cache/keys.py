"""Redis key namespace.

Every key is built here so the namespace stays greppable and a prefix change
is a one-line edit. The prefix differs from the Accounting module's so the two
can share a Redis instance without colliding.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

PREFIX = "erphrm"


def _digest(value: str) -> str:
    """Hash free-form input so keys stay bounded and leak nothing."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def rate_limit_key(identity: str, window: int) -> str:
    return f"{PREFIX}:ratelimit:{_digest(identity)}:{window}"


def user_permissions_key(user_id: UUID) -> str:
    return f"{PREFIX}:perms:{user_id}"


def company_key(company_id: UUID) -> str:
    return f"{PREFIX}:company:{company_id}"


def holidays_key(company_id: UUID, year: int) -> str:
    return f"{PREFIX}:holidays:{company_id}:{year}"


def formulas_key() -> str:
    return f"{PREFIX}:formulas"


PERMISSIONS_TTL_SECONDS = 300
CONFIG_TTL_SECONDS = 600
HOLIDAYS_TTL_SECONDS = 3600

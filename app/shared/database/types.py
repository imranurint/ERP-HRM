"""Column type helpers used by every module's models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import Enum


def status_enum(enum_type: type[StrEnum], name: str, length: int = 24) -> Enum:
    """A string-backed enum column with a CHECK constraint.

    `native_enum=False` throughout: adding a status later is a constraint swap
    rather than an `ALTER TYPE`, and the values stay readable in raw SQL — the
    same reasoning the Accounting and Core modules use.
    """
    values_callable: Any = lambda enum: [member.value for member in enum]  # noqa: E731
    return Enum(
        enum_type,
        name=name,
        native_enum=False,
        length=length,
        create_constraint=True,
        values_callable=values_callable,
    )

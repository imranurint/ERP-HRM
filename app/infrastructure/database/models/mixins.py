"""Model mixins and shared column helpers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.orm import Mapped, mapped_column


class CreatedAtMixin:
    """For append-only tables, which have no meaningful `updated_at`."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


def status_enum(enum_type: type[StrEnum], name: str, length: int = 24) -> Enum:
    """A string-backed enum column with a CHECK constraint.

    `native_enum=False` throughout: adding a status later is a constraint swap
    rather than an `ALTER TYPE`, and the values stay readable in raw SQL — the
    same reasoning the Accounting module uses.
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

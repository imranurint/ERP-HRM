"""Transactional outbox table.

An event row is written in the *same* transaction as the HR change it
describes, so either both land or neither does. Publishing to an external bus
happens later, outside that transaction, and is deliberately not this module's
problem: a broker outage must never block a leave approval or a payroll run.

`PayrollApproved` events carry a journal-ready payload, which is how payroll
reaches the Accounting module's `CreateJournalCommand` without HRM importing
anything from it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.shared.enums import OutboxStatus
from app.infrastructure.database.base import Base, UUIDPrimaryKeyMixin
from app.infrastructure.database.models.mixins import CreatedAtMixin, status_enum


class OutboxEvent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "outbox_events"

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(nullable=False)
    company_id: Mapped[UUID | None] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[OutboxStatus] = mapped_column(
        status_enum(OutboxStatus, "outbox_status", 16),
        nullable=False,
        default=OutboxStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The relay only ever scans PENDING rows; keep that index tiny.
        Index(
            "ix_outbox_events_pending",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
        Index("ix_outbox_events_type", "event_type"),
    )

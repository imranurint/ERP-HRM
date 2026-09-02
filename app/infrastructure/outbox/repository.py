"""Outbox repository.

`enqueue` runs inside the HR transaction. `claim_pending` /
`mark_processed` belong to a relay process and are intentionally separate: no
external publishing ever participates in a payroll or leave transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.shared.enums import OutboxStatus
from app.domain.shared.events import DomainEvent
from app.infrastructure.outbox.models import OutboxEvent


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def enqueue(self, event: DomainEvent) -> OutboxEvent:
        """Stage an event row; it commits with the HR change."""
        row = OutboxEvent(
            event_type=type(event).event_type,
            aggregate_type=type(event).aggregate_type,
            aggregate_id=event.aggregate_id,
            company_id=event.company_id,
            payload=event.payload(),
            status=OutboxStatus.PENDING,
        )
        self.session.add(row)
        return row

    async def claim_pending(self, limit: int = 100) -> Sequence[OutboxEvent]:
        """Take a batch of pending events with `SKIP LOCKED`.

        Several relay workers can run concurrently without handing the same
        event to two of them.
        """
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.PENDING)
            .order_by(OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        for row in rows:
            row.status = OutboxStatus.PROCESSING
        return rows

    async def mark_processed(self, event_id: object) -> None:
        row = await self.session.get(OutboxEvent, event_id)
        if row is not None:
            row.status = OutboxStatus.PROCESSED
            row.processed_at = datetime.now(UTC)

    async def mark_failed(self, event_id: object, error: str) -> None:
        row = await self.session.get(OutboxEvent, event_id)
        if row is not None:
            row.status = OutboxStatus.FAILED
            row.attempts += 1
            row.last_error = error[:1000]

    async def count_pending(self) -> int:
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.PENDING)
        )
        return int(await self.session.scalar(stmt) or 0)

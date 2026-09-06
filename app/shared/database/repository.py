"""Repository base class.

Repositories own SQL. Application services own orchestration. Nothing above
this layer imports SQLAlchemy, which is what keeps the domain testable
without a database and lets the persistence strategy change without a
rewrite of the HR rules.

Repositories never commit — the Unit of Work decides transaction boundaries.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.database.base import Base


class BaseRepository[ModelT: Base]:
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- reads -------------------------------------------------------------
    async def get(self, entity_id: UUID) -> ModelT | None:
        return await self.session.get(self.model, entity_id)

    async def list_all(self, *criteria: Any) -> Sequence[ModelT]:
        stmt = select(self.model)
        if criteria:
            stmt = stmt.where(*criteria)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def count(self, stmt: Select[Any]) -> int:
        """Count the rows a SELECT would return, without fetching them."""
        subquery = stmt.order_by(None).limit(None).offset(None).subquery()
        total = await self.session.scalar(select(func.count()).select_from(subquery))
        return int(total or 0)

    async def exists(self, *criteria: Any) -> bool:
        stmt = select(func.count()).select_from(self.model).where(*criteria).limit(1)
        return bool(await self.session.scalar(stmt))

    # --- writes ------------------------------------------------------------
    def add(self, instance: ModelT) -> ModelT:
        self.session.add(instance)
        return instance

    def add_all(self, instances: Sequence[ModelT]) -> None:
        self.session.add_all(list(instances))

    async def delete(self, instance: ModelT) -> None:
        await self.session.delete(instance)

    async def flush(self) -> None:
        """Push pending changes so the database assigns/validates them.

        Used inside the payroll transaction to surface constraint violations
        (and let us translate them into domain errors) before commit.
        """
        await self.session.flush()

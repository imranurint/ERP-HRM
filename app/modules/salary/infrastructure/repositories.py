"""Salary component and salary structure repositories."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.core.pagination import PageParams
from app.modules.payroll.domain.enums import SalaryStructureStatus
from app.modules.salary.infrastructure.models import (
    EmployeeSalaryComponent,
    EmployeeSalaryStructure,
    SalaryComponent,
)
from app.shared.database.repository import BaseRepository


class SalaryComponentRepository(BaseRepository[SalaryComponent]):
    model = SalaryComponent

    async def get_scoped(self, company_id: UUID, component_id: UUID) -> SalaryComponent | None:
        stmt = select(SalaryComponent).where(
            SalaryComponent.id == component_id, SalaryComponent.company_id == company_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_code(self, company_id: UUID, code: str) -> SalaryComponent | None:
        stmt = select(SalaryComponent).where(
            SalaryComponent.company_id == company_id,
            SalaryComponent.component_code == code.strip().upper(),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_company(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[Sequence[SalaryComponent], int]:
        stmt = select(SalaryComponent).where(SalaryComponent.company_id == company_id)
        if is_active is not None:
            stmt = stmt.where(SalaryComponent.is_active == is_active)
        total = await self.count(stmt)
        stmt = (
            stmt.order_by(SalaryComponent.sequence, SalaryComponent.component_code)
            .offset(params.offset)
            .limit(params.limit)
        )
        return (await self.session.execute(stmt)).scalars().all(), total


class SalaryStructureRepository(BaseRepository[EmployeeSalaryStructure]):
    model = EmployeeSalaryStructure

    async def get_scoped(
        self, company_id: UUID, structure_id: UUID
    ) -> EmployeeSalaryStructure | None:
        stmt = (
            select(EmployeeSalaryStructure)
            .where(
                EmployeeSalaryStructure.id == structure_id,
                EmployeeSalaryStructure.company_id == company_id,
            )
            .options(
                selectinload(EmployeeSalaryStructure.components).joinedload(
                    EmployeeSalaryComponent.component
                )
            )
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()

    async def effective_on(self, employee_id: UUID, as_of: date) -> EmployeeSalaryStructure | None:
        """The one ACTIVE structure covering `as_of`.

        The GiST exclusion constraint guarantees there is at most one, so this
        can safely return a single row rather than picking from a list.
        """
        stmt = (
            select(EmployeeSalaryStructure)
            .where(
                EmployeeSalaryStructure.employee_id == employee_id,
                EmployeeSalaryStructure.status == SalaryStructureStatus.ACTIVE,
                EmployeeSalaryStructure.effective_from <= as_of,
                or_(
                    EmployeeSalaryStructure.effective_to.is_(None),
                    EmployeeSalaryStructure.effective_to >= as_of,
                ),
            )
            .options(
                selectinload(EmployeeSalaryStructure.components).joinedload(
                    EmployeeSalaryComponent.component
                )
            )
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()

    async def effective_on_bulk(
        self, employee_ids: Sequence[UUID], as_of: date
    ) -> dict[UUID, EmployeeSalaryStructure]:
        """Structures for many employees in one query.

        Payroll for a thousand people must not issue a thousand round-trips —
        this is the N+1 that would otherwise dominate a payroll run.
        """
        if not employee_ids:
            return {}
        stmt = (
            select(EmployeeSalaryStructure)
            .where(
                EmployeeSalaryStructure.employee_id.in_(list(employee_ids)),
                EmployeeSalaryStructure.status == SalaryStructureStatus.ACTIVE,
                EmployeeSalaryStructure.effective_from <= as_of,
                or_(
                    EmployeeSalaryStructure.effective_to.is_(None),
                    EmployeeSalaryStructure.effective_to >= as_of,
                ),
            )
            .options(
                selectinload(EmployeeSalaryStructure.components).joinedload(
                    EmployeeSalaryComponent.component
                )
            )
        )
        rows = (await self.session.execute(stmt)).unique().scalars().all()
        return {row.employee_id: row for row in rows}

    async def list_for_employee(self, employee_id: UUID) -> Sequence[EmployeeSalaryStructure]:
        stmt = (
            select(EmployeeSalaryStructure)
            .where(EmployeeSalaryStructure.employee_id == employee_id)
            .order_by(EmployeeSalaryStructure.effective_from.desc())
            .options(
                selectinload(EmployeeSalaryStructure.components).joinedload(
                    EmployeeSalaryComponent.component
                )
            )
        )
        return (await self.session.execute(stmt)).unique().scalars().all()

    async def existing_ranges(
        self, employee_id: UUID, *, exclude_id: UUID | None = None
    ) -> list[tuple[date, date | None]]:
        """Date ranges already claimed by an ACTIVE structure.

        Only ACTIVE rows are considered, matching the partial exclusion
        constraint: a DRAFT structure is a proposal and must be allowed to
        overlap the one currently in force.
        """
        stmt = select(
            EmployeeSalaryStructure.effective_from, EmployeeSalaryStructure.effective_to
        ).where(
            EmployeeSalaryStructure.employee_id == employee_id,
            EmployeeSalaryStructure.status == SalaryStructureStatus.ACTIVE,
        )
        if exclude_id is not None:
            stmt = stmt.where(EmployeeSalaryStructure.id != exclude_id)
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

"""Employee, contract and lifecycle repositories."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import joinedload

from app.core.pagination import PageParams
from app.modules.employees.domain.enums import ContractStatus, EmploymentStatus
from app.modules.employees.infrastructure.models import (
    Employee,
    EmployeeLifecycleEvent,
    EmploymentContract,
)
from app.shared.database.repository import BaseRepository

MAX_HIERARCHY_DEPTH = 64


class EmployeeRepository(BaseRepository[Employee]):
    model = Employee

    # --- reads -------------------------------------------------------------
    async def get_scoped(self, company_id: UUID, employee_id: UUID) -> Employee | None:
        """Fetch by id *and* company. Never fetch an employee by id alone."""
        stmt = select(Employee).where(Employee.id == employee_id, Employee.company_id == company_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_for_update(self, company_id: UUID, employee_id: UUID) -> Employee | None:
        """Row-locked fetch, for lifecycle transitions.

        Termination and status changes read-then-write, so the row is locked
        for the duration: two concurrent terminations must not both succeed
        and produce two lifecycle events for the same departure.
        """
        stmt = (
            select(Employee)
            .where(Employee.id == employee_id, Employee.company_id == company_id)
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_number(self, company_id: UUID, employee_number: str) -> Employee | None:
        stmt = select(Employee).where(
            Employee.company_id == company_id,
            Employee.employee_number == employee_number.strip().upper(),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_work_email(self, company_id: UUID, email: str) -> Employee | None:
        stmt = select(Employee).where(
            Employee.company_id == company_id,
            func.lower(Employee.work_email) == email.strip().lower(),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_user_id(self, user_id: UUID) -> Employee | None:
        """The employee record behind a login. Powers self-service scoping."""
        stmt = select(Employee).where(Employee.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_paginated(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        department_id: UUID | None = None,
        designation_id: UUID | None = None,
        location_id: UUID | None = None,
        manager_id: UUID | None = None,
        employment_status: EmploymentStatus | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> tuple[Sequence[Employee], int]:
        stmt = self._filtered(
            company_id,
            department_id=department_id,
            designation_id=designation_id,
            location_id=location_id,
            manager_id=manager_id,
            employment_status=employment_status,
            is_active=is_active,
            search=search,
        )
        total = await self.count(stmt)
        stmt = (
            stmt.order_by(Employee.employee_number)
            .offset(params.offset)
            .limit(params.limit)
            .options(joinedload(Employee.manager))
        )
        rows = (await self.session.execute(stmt)).unique().scalars().all()
        return rows, total

    def _filtered(
        self,
        company_id: UUID,
        *,
        department_id: UUID | None = None,
        designation_id: UUID | None = None,
        location_id: UUID | None = None,
        manager_id: UUID | None = None,
        employment_status: EmploymentStatus | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> Select[Any]:
        stmt = select(Employee).where(Employee.company_id == company_id)
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        if designation_id is not None:
            stmt = stmt.where(Employee.designation_id == designation_id)
        if location_id is not None:
            stmt = stmt.where(Employee.location_id == location_id)
        if manager_id is not None:
            stmt = stmt.where(Employee.manager_id == manager_id)
        if employment_status is not None:
            stmt = stmt.where(Employee.employment_status == employment_status)
        if is_active is not None:
            stmt = stmt.where(Employee.is_active == is_active)
        if search:
            pattern = f"%{search.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Employee.first_name).like(pattern),
                    func.lower(Employee.last_name).like(pattern),
                    func.lower(Employee.work_email).like(pattern),
                    func.lower(Employee.employee_number).like(pattern),
                )
            )
        return stmt

    async def list_payable(
        self, company_id: UUID, *, as_of: date, employee_ids: Sequence[UUID] | None = None
    ) -> Sequence[Employee]:
        """Employees eligible for a payroll run covering `as_of`.

        Excludes anybody who had already left before the period and anybody
        not yet hired — paying either is a real and expensive mistake.
        """
        stmt = select(Employee).where(
            Employee.company_id == company_id,
            Employee.hire_date <= as_of,
            Employee.employment_status != EmploymentStatus.INACTIVE,
            or_(Employee.termination_date.is_(None), Employee.termination_date >= as_of),
        )
        if employee_ids:
            stmt = stmt.where(Employee.id.in_(list(employee_ids)))
        return (await self.session.execute(stmt.order_by(Employee.employee_number))).scalars().all()

    async def manager_chain_ids(self, employee_id: UUID) -> list[UUID]:
        """Walk up the reporting line. Feeds the manager-cycle check."""
        chain: list[UUID] = []
        current: UUID | None = employee_id
        for _ in range(MAX_HIERARCHY_DEPTH):
            if current is None:
                break
            manager = await self.session.scalar(
                select(Employee.manager_id).where(Employee.id == current)
            )
            if manager is None:
                break
            chain.append(manager)
            current = manager
        return chain

    async def direct_report_count(self, manager_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(Employee)
            .where(Employee.manager_id == manager_id, Employee.is_active.is_(True))
        )
        return int(await self.session.scalar(stmt) or 0)

    async def headcount_by_department(
        self, company_id: UUID, *, as_of: date
    ) -> Sequence[tuple[UUID | None, int]]:
        stmt = (
            select(Employee.department_id, func.count())
            .where(
                Employee.company_id == company_id,
                Employee.hire_date <= as_of,
                or_(Employee.termination_date.is_(None), Employee.termination_date >= as_of),
                Employee.employment_status.in_(
                    [
                        EmploymentStatus.ACTIVE,
                        EmploymentStatus.PROBATION,
                        EmploymentStatus.ON_LEAVE,
                    ]
                ),
            )
            .group_by(Employee.department_id)
        )
        return [(row[0], int(row[1])) for row in (await self.session.execute(stmt)).all()]


class EmploymentContractRepository(BaseRepository[EmploymentContract]):
    model = EmploymentContract

    async def get_scoped(self, company_id: UUID, contract_id: UUID) -> EmploymentContract | None:
        stmt = select(EmploymentContract).where(
            EmploymentContract.id == contract_id,
            EmploymentContract.company_id == company_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_active(self, employee_id: UUID) -> EmploymentContract | None:
        stmt = select(EmploymentContract).where(
            EmploymentContract.employee_id == employee_id,
            EmploymentContract.status == ContractStatus.ACTIVE,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_employee(self, employee_id: UUID) -> Sequence[EmploymentContract]:
        stmt = (
            select(EmploymentContract)
            .where(EmploymentContract.employee_id == employee_id)
            .order_by(EmploymentContract.start_date.desc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def expire_lapsed(self, company_id: UUID, *, as_of: date) -> int:
        """Move ACTIVE contracts past their end date to EXPIRED.

        Idempotent, and safe to run repeatedly. Contract expiry is *also*
        derived on read (`contract_status_on`), so a missed run degrades
        reporting rather than correctness.
        """
        stmt = select(EmploymentContract).where(
            EmploymentContract.company_id == company_id,
            EmploymentContract.status == ContractStatus.ACTIVE,
            EmploymentContract.end_date.is_not(None),
            EmploymentContract.end_date < as_of,
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        for row in rows:
            row.status = ContractStatus.EXPIRED
        return len(rows)


class LifecycleEventRepository(BaseRepository[EmployeeLifecycleEvent]):
    model = EmployeeLifecycleEvent

    async def list_for_employee(
        self, employee_id: UUID, params: PageParams
    ) -> tuple[Sequence[EmployeeLifecycleEvent], int]:
        stmt = select(EmployeeLifecycleEvent).where(
            EmployeeLifecycleEvent.employee_id == employee_id
        )
        total = await self.count(stmt)
        stmt = (
            stmt.order_by(
                EmployeeLifecycleEvent.effective_date.desc(),
                EmployeeLifecycleEvent.created_at.desc(),
            )
            .offset(params.offset)
            .limit(params.limit)
        )
        return (await self.session.execute(stmt)).scalars().all(), total

    async def list_for_company(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        event_type: str | None = None,
    ) -> tuple[Sequence[EmployeeLifecycleEvent], int]:
        stmt = select(EmployeeLifecycleEvent).where(EmployeeLifecycleEvent.company_id == company_id)
        if start_date is not None:
            stmt = stmt.where(EmployeeLifecycleEvent.effective_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(EmployeeLifecycleEvent.effective_date <= end_date)
        if event_type is not None:
            stmt = stmt.where(EmployeeLifecycleEvent.event_type == event_type)
        total = await self.count(stmt)
        stmt = (
            stmt.order_by(EmployeeLifecycleEvent.effective_date.desc())
            .offset(params.offset)
            .limit(params.limit)
        )
        return (await self.session.execute(stmt)).scalars().all(), total

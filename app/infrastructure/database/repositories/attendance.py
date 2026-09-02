"""Shift and attendance repositories."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from app.core.pagination import PageParams
from app.domain.attendance.enums import AttendanceStatus
from app.infrastructure.database.models.attendance import (
    AttendanceRecord,
    EmployeeShiftAssignment,
    Shift,
)
from app.infrastructure.database.repositories.base import BaseRepository


class ShiftRepository(BaseRepository[Shift]):
    model = Shift

    async def get_scoped(self, company_id: UUID, shift_id: UUID) -> Shift | None:
        stmt = select(Shift).where(Shift.id == shift_id, Shift.company_id == company_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_code(self, company_id: UUID, code: str) -> Shift | None:
        stmt = select(Shift).where(
            Shift.company_id == company_id, Shift.shift_code == code.strip().upper()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_company(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[Sequence[Shift], int]:
        stmt = select(Shift).where(Shift.company_id == company_id)
        if is_active is not None:
            stmt = stmt.where(Shift.is_active == is_active)
        total = await self.count(stmt)
        stmt = stmt.order_by(Shift.shift_code).offset(params.offset).limit(params.limit)
        return (await self.session.execute(stmt)).scalars().all(), total


class ShiftAssignmentRepository(BaseRepository[EmployeeShiftAssignment]):
    model = EmployeeShiftAssignment

    async def effective_on(self, employee_id: UUID, as_of: date) -> EmployeeShiftAssignment | None:
        """The one assignment covering `as_of`, with its shift eagerly loaded."""
        stmt = (
            select(EmployeeShiftAssignment)
            .where(
                EmployeeShiftAssignment.employee_id == employee_id,
                EmployeeShiftAssignment.effective_from <= as_of,
                or_(
                    EmployeeShiftAssignment.effective_to.is_(None),
                    EmployeeShiftAssignment.effective_to >= as_of,
                ),
            )
            .options(joinedload(EmployeeShiftAssignment.shift))
        )
        return (await self.session.execute(stmt)).unique().scalar_one_or_none()

    async def effective_on_bulk(
        self, employee_ids: Sequence[UUID], as_of: date
    ) -> dict[UUID, EmployeeShiftAssignment]:
        if not employee_ids:
            return {}
        stmt = (
            select(EmployeeShiftAssignment)
            .where(
                EmployeeShiftAssignment.employee_id.in_(list(employee_ids)),
                EmployeeShiftAssignment.effective_from <= as_of,
                or_(
                    EmployeeShiftAssignment.effective_to.is_(None),
                    EmployeeShiftAssignment.effective_to >= as_of,
                ),
            )
            .options(joinedload(EmployeeShiftAssignment.shift))
        )
        rows = (await self.session.execute(stmt)).unique().scalars().all()
        return {row.employee_id: row for row in rows}

    async def existing_ranges(
        self, employee_id: UUID, *, exclude_id: UUID | None = None
    ) -> list[tuple[date, date | None]]:
        stmt = select(
            EmployeeShiftAssignment.effective_from, EmployeeShiftAssignment.effective_to
        ).where(EmployeeShiftAssignment.employee_id == employee_id)
        if exclude_id is not None:
            stmt = stmt.where(EmployeeShiftAssignment.id != exclude_id)
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]


class AttendanceRepository(BaseRepository[AttendanceRecord]):
    model = AttendanceRecord

    async def get_for_day(
        self, employee_id: UUID, attendance_date: date
    ) -> AttendanceRecord | None:
        stmt = select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id,
            AttendanceRecord.attendance_date == attendance_date,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_for_day_locked(
        self, employee_id: UUID, attendance_date: date
    ) -> AttendanceRecord | None:
        """Row-locked, for check-out.

        Check-out is read-then-write. Two devices posting a check-out at once
        would otherwise both read "no check-out yet" and race.
        """
        stmt = (
            select(AttendanceRecord)
            .where(
                AttendanceRecord.employee_id == employee_id,
                AttendanceRecord.attendance_date == attendance_date,
            )
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_scoped(self, company_id: UUID, record_id: UUID) -> AttendanceRecord | None:
        stmt = select(AttendanceRecord).where(
            AttendanceRecord.id == record_id, AttendanceRecord.company_id == company_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_paginated(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        employee_id: UUID | None = None,
        department_id: UUID | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        status: AttendanceStatus | None = None,
    ) -> tuple[Sequence[AttendanceRecord], int]:
        from app.infrastructure.database.models.employee import Employee

        stmt = select(AttendanceRecord).where(AttendanceRecord.company_id == company_id)
        if employee_id is not None:
            stmt = stmt.where(AttendanceRecord.employee_id == employee_id)
        if department_id is not None:
            stmt = stmt.join(Employee, Employee.id == AttendanceRecord.employee_id).where(
                Employee.department_id == department_id
            )
        if start_date is not None:
            stmt = stmt.where(AttendanceRecord.attendance_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(AttendanceRecord.attendance_date <= end_date)
        if status is not None:
            stmt = stmt.where(AttendanceRecord.status == status)

        total = await self.count(stmt)
        stmt = (
            stmt.order_by(AttendanceRecord.attendance_date.desc(), AttendanceRecord.employee_id)
            .offset(params.offset)
            .limit(params.limit)
        )
        return (await self.session.execute(stmt)).scalars().all(), total

    async def list_for_range(
        self,
        company_id: UUID,
        employee_ids: Sequence[UUID],
        start_date: date,
        end_date: date,
    ) -> Sequence[AttendanceRecord]:
        """Every record for a set of employees over a range — one query.

        Payroll calls this once per run rather than once per employee.
        """
        if not employee_ids:
            return []
        stmt = (
            select(AttendanceRecord)
            .where(
                AttendanceRecord.company_id == company_id,
                AttendanceRecord.employee_id.in_(list(employee_ids)),
                AttendanceRecord.attendance_date >= start_date,
                AttendanceRecord.attendance_date <= end_date,
            )
            .order_by(AttendanceRecord.employee_id, AttendanceRecord.attendance_date)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def summarise(
        self,
        company_id: UUID,
        start_date: date,
        end_date: date,
        *,
        employee_id: UUID | None = None,
        department_id: UUID | None = None,
    ) -> Sequence[tuple[UUID, AttendanceStatus, int, int, int, int]]:
        """Per-employee, per-status totals. Backs the attendance reports."""
        from app.infrastructure.database.models.employee import Employee

        stmt = (
            select(
                AttendanceRecord.employee_id,
                AttendanceRecord.status,
                func.count(),
                func.coalesce(func.sum(AttendanceRecord.working_minutes), 0),
                func.coalesce(func.sum(AttendanceRecord.overtime_minutes), 0),
                func.coalesce(func.sum(AttendanceRecord.late_minutes), 0),
            )
            .where(
                AttendanceRecord.company_id == company_id,
                AttendanceRecord.attendance_date >= start_date,
                AttendanceRecord.attendance_date <= end_date,
            )
            .group_by(AttendanceRecord.employee_id, AttendanceRecord.status)
        )
        if employee_id is not None:
            stmt = stmt.where(AttendanceRecord.employee_id == employee_id)
        if department_id is not None:
            stmt = stmt.join(Employee, Employee.id == AttendanceRecord.employee_id).where(
                Employee.department_id == department_id
            )
        rows = (await self.session.execute(stmt)).all()
        return [
            (row[0], AttendanceStatus(row[1]), int(row[2]), int(row[3]), int(row[4]), int(row[5]))
            for row in rows
        ]

    async def late_records(
        self,
        company_id: UUID,
        start_date: date,
        end_date: date,
        params: PageParams,
        *,
        minimum_minutes: int = 1,
    ) -> tuple[Sequence[AttendanceRecord], int]:
        stmt = select(AttendanceRecord).where(
            AttendanceRecord.company_id == company_id,
            AttendanceRecord.attendance_date >= start_date,
            AttendanceRecord.attendance_date <= end_date,
            AttendanceRecord.late_minutes >= minimum_minutes,
        )
        total = await self.count(stmt)
        stmt = (
            stmt.order_by(AttendanceRecord.late_minutes.desc())
            .offset(params.offset)
            .limit(params.limit)
        )
        return (await self.session.execute(stmt)).scalars().all(), total

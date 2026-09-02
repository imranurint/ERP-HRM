"""Attendance service.

Two things here are easy to get wrong and are handled explicitly:

**Which day does a check-in belong to?** The instant is stored in UTC, but the
`attendance_date` it is filed under is the calendar date in the *company's*
timezone. An 01:00 UTC check-in belongs to a different day in Manila than in
London, and getting that wrong misfiles a whole office's attendance.

**Payroll-period locking.** Once a payroll period reaches APPROVED, the
attendance underneath it is frozen. Editing it afterwards would silently
invalidate payslips that have already been signed off.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.application.attendance.schemas import (
    AttendanceRead,
    AttendanceSummaryRow,
    CheckInRequest,
    CheckOutRequest,
    ManualAttendanceRequest,
    ShiftAssignmentCreate,
    ShiftAssignmentRead,
    ShiftCreate,
    ShiftRead,
    ShiftUpdate,
)
from app.application.context import ActorContext
from app.core.config import settings
from app.core.dates import (
    as_utc,
    combine_shift_datetime,
    is_weekend,
    local_date,
    spans_midnight,
    utcnow,
)
from app.core.exceptions import DuplicateResourceError, ValidationError
from app.core.pagination import Page, PageParams
from app.domain.attendance.enums import AttendanceSource, AttendanceStatus
from app.domain.attendance.exceptions import (
    AlreadyCheckedOutError,
    AttendanceNotFoundError,
    NotCheckedInError,
    ShiftNotFoundError,
)
from app.domain.attendance.rules import (
    ShiftDefinition,
    compute,
    ensure_shift_valid,
    payable_day_fraction,
)
from app.domain.employees.exceptions import EmployeeNotFoundError
from app.domain.payroll.rules import ensure_period_unlocked_for_source_data
from app.domain.shared.enums import AuditAction
from app.infrastructure.database.models.attendance import (
    AttendanceRecord,
    EmployeeShiftAssignment,
    Shift,
)
from app.infrastructure.database.models.employee import Employee
from app.infrastructure.database.unit_of_work import UnitOfWork
from app.infrastructure.observability import metrics


class AttendanceService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    # --------------------------------------------------------------- shifts
    async def create_shift(
        self, company_id: UUID, payload: ShiftCreate, actor: ActorContext
    ) -> ShiftRead:
        if await self.uow.shifts.get_by_code(company_id, payload.shift_code):
            raise DuplicateResourceError(
                "That shift code already exists for this company.",
                details={"shift_code": payload.shift_code},
            )
        ensure_shift_valid(payload.start_time, payload.end_time, payload.working_hours)

        shift = Shift(
            company_id=company_id,
            shift_code=payload.shift_code,
            name=payload.name,
            start_time=payload.start_time,
            end_time=payload.end_time,
            grace_minutes=payload.grace_minutes,
            working_hours=payload.working_hours,
            is_active=True,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.shifts.add(shift)
        await self.uow.flush()

        self._audit(
            AuditAction.SHIFT_CREATED,
            "Shift",
            shift.id,
            company_id,
            actor,
            new_data={
                "shift_code": shift.shift_code,
                "start_time": shift.start_time.isoformat(),
                "end_time": shift.end_time.isoformat(),
                "overnight": spans_midnight(shift.start_time, shift.end_time),
            },
        )
        await self.uow.commit()
        return self._shift_read(shift)

    async def update_shift(
        self, company_id: UUID, shift_id: UUID, payload: ShiftUpdate, actor: ActorContext
    ) -> ShiftRead:
        shift = await self._require_shift(company_id, shift_id)
        changes = payload.model_dump(exclude_unset=True)
        merged_start = changes.get("start_time", shift.start_time)
        merged_end = changes.get("end_time", shift.end_time)
        merged_hours = changes.get("working_hours", Decimal(str(shift.working_hours)))
        ensure_shift_valid(merged_start, merged_end, Decimal(str(merged_hours)))

        for field, value in changes.items():
            setattr(shift, field, value)
        shift.updated_by = actor.user_id
        await self.uow.commit()
        return self._shift_read(shift)

    async def list_shifts(
        self, company_id: UUID, params: PageParams, *, is_active: bool | None = None
    ) -> Page[ShiftRead]:
        rows, total = await self.uow.shifts.list_for_company(
            company_id, params, is_active=is_active
        )
        return Page.build([self._shift_read(r) for r in rows], params, total=total)

    async def assign_shift(
        self, company_id: UUID, payload: ShiftAssignmentCreate, actor: ActorContext
    ) -> ShiftAssignmentRead:
        """Assign a shift over a date range.

        Overlapping assignments are refused by a GiST exclusion constraint —
        "which shift were they on?" has to have exactly one answer.
        """
        employee = await self._require_employee(company_id, payload.employee_id)
        shift = await self._require_shift(company_id, payload.shift_id)
        if payload.effective_to is not None and payload.effective_to < payload.effective_from:
            raise ValidationError(
                "Shift assignment end date cannot precede its start date.",
                details={
                    "effective_from": payload.effective_from.isoformat(),
                    "effective_to": payload.effective_to.isoformat(),
                },
            )

        assignment = EmployeeShiftAssignment(
            company_id=company_id,
            employee_id=employee.id,
            shift_id=shift.id,
            effective_from=payload.effective_from,
            effective_to=payload.effective_to,
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        self.uow.shift_assignments.add(assignment)
        await self.uow.flush()

        self._audit(
            AuditAction.SHIFT_ASSIGNED,
            "EmployeeShiftAssignment",
            assignment.id,
            company_id,
            actor,
            new_data={
                "employee_id": str(employee.id),
                "shift_code": shift.shift_code,
                "effective_from": assignment.effective_from.isoformat(),
            },
        )
        await self.uow.commit()
        return ShiftAssignmentRead.model_validate(assignment)

    # ------------------------------------------------------------ check in/out
    async def check_in(
        self, company_id: UUID, payload: CheckInRequest, employee_id: UUID, actor: ActorContext
    ) -> AttendanceRead:
        employee = await self._require_employee(company_id, employee_id)
        moment = as_utc(payload.at) if payload.at else utcnow()
        timezone_name = await self._timezone_for(employee)
        day = local_date(moment, timezone_name)

        await self._ensure_period_open(company_id, day, what="Attendance")

        existing = await self.uow.attendance.get_for_day_locked(employee.id, day)
        if existing is not None and existing.check_in is not None:
            # Not an error worth failing on: a second badge swipe is a normal
            # human action. Return the record they already have.
            return AttendanceRead.model_validate(existing)

        shift, definition = await self._resolve_shift(employee, day)
        scheduled_start, scheduled_end = self._scheduled_window(day, definition, timezone_name)
        computation = compute(
            shift=definition,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            check_in=moment,
            check_out=None,
            overtime_threshold_minutes=settings.attendance_overtime_threshold_minutes,
            non_working_day_status=await self._non_working_status(employee, day),
        )

        record = existing or AttendanceRecord(
            company_id=company_id,
            employee_id=employee.id,
            attendance_date=day,
            created_by=actor.user_id,
        )
        record.shift_id = shift.id if shift else None
        record.check_in = moment
        record.status = computation.status
        record.working_minutes = computation.working_minutes
        record.late_minutes = computation.late_minutes
        record.early_leave_minutes = computation.early_leave_minutes
        record.overtime_minutes = computation.overtime_minutes
        record.source = payload.source
        record.notes = payload.notes
        record.updated_by = actor.user_id
        if existing is None:
            self.uow.attendance.add(record)
        await self.uow.flush()

        self._audit(
            AuditAction.ATTENDANCE_CHECKED_IN,
            "AttendanceRecord",
            record.id,
            company_id,
            actor,
            new_data={
                "employee_id": str(employee.id),
                "attendance_date": day.isoformat(),
                "check_in": moment.isoformat(),
                "status": str(record.status),
            },
        )
        await self.uow.commit()
        await self.uow.refresh(record)

        metrics.attendance_records_total.labels(str(company_id), str(payload.source)).inc()
        return AttendanceRead.model_validate(record)

    async def check_out(
        self, company_id: UUID, payload: CheckOutRequest, employee_id: UUID, actor: ActorContext
    ) -> AttendanceRead:
        employee = await self._require_employee(company_id, employee_id)
        moment = as_utc(payload.at) if payload.at else utcnow()
        timezone_name = await self._timezone_for(employee)
        day = local_date(moment, timezone_name)

        # An overnight worker checks out on the *next* calendar day, so look
        # for yesterday's open record before giving up.
        record = await self.uow.attendance.get_for_day_locked(employee.id, day)
        if record is None or record.check_in is None:
            from datetime import timedelta

            previous_day = day - timedelta(days=1)
            candidate = await self.uow.attendance.get_for_day_locked(employee.id, previous_day)
            if (
                candidate is not None
                and candidate.check_in is not None
                and (candidate.check_out is None)
            ):
                record = candidate
                day = previous_day

        if record is None or record.check_in is None:
            raise NotCheckedInError(
                details={"employee_id": str(employee.id), "attendance_date": day.isoformat()}
            )
        if record.check_out is not None:
            raise AlreadyCheckedOutError(
                details={
                    "attendance_date": day.isoformat(),
                    "check_out": record.check_out.isoformat(),
                }
            )

        await self._ensure_period_open(company_id, day, what="Attendance")

        _, definition = await self._resolve_shift(employee, day)
        scheduled_start, scheduled_end = self._scheduled_window(day, definition, timezone_name)
        computation = compute(
            shift=definition,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            check_in=record.check_in,
            check_out=moment,
            overtime_threshold_minutes=settings.attendance_overtime_threshold_minutes,
            non_working_day_status=await self._non_working_status(employee, day),
        )

        previous = {"check_out": None, "status": str(record.status)}
        record.check_out = moment
        record.status = computation.status
        record.working_minutes = computation.working_minutes
        record.late_minutes = computation.late_minutes
        record.early_leave_minutes = computation.early_leave_minutes
        record.overtime_minutes = computation.overtime_minutes
        if payload.notes:
            record.notes = payload.notes
        record.updated_by = actor.user_id

        self._audit(
            AuditAction.ATTENDANCE_CHECKED_OUT,
            "AttendanceRecord",
            record.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={
                "check_out": moment.isoformat(),
                "status": str(record.status),
                "working_minutes": record.working_minutes,
                "overtime_minutes": record.overtime_minutes,
            },
        )
        await self.uow.commit()
        return AttendanceRead.model_validate(record)

    # -------------------------------------------------------------- manual
    async def record_manual(
        self, company_id: UUID, payload: ManualAttendanceRequest, actor: ActorContext
    ) -> AttendanceRead:
        """Create or amend a day by hand.

        Refused inside an approved payroll period: the payslips derived from
        it have already been signed off.
        """
        employee = await self._require_employee(company_id, payload.employee_id)
        await self._ensure_period_open(company_id, payload.attendance_date, what="Attendance")

        timezone_name = await self._timezone_for(employee)
        shift, definition = await self._resolve_shift(employee, payload.attendance_date)
        scheduled_start, scheduled_end = self._scheduled_window(
            payload.attendance_date, definition, timezone_name
        )

        check_in = as_utc(payload.check_in) if payload.check_in else None
        check_out = as_utc(payload.check_out) if payload.check_out else None
        if check_out is not None and check_in is None:
            raise ValidationError(
                "A check-out requires a check-in.",
                details={"attendance_date": payload.attendance_date.isoformat()},
            )

        override = payload.status
        computation = compute(
            shift=definition,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            check_in=check_in,
            check_out=check_out,
            overtime_threshold_minutes=settings.attendance_overtime_threshold_minutes,
            non_working_day_status=(
                override
                if override in _NON_WORKING_STATUSES
                else await self._non_working_status(employee, payload.attendance_date)
            ),
        )

        existing = await self.uow.attendance.get_for_day_locked(
            employee.id, payload.attendance_date
        )
        is_amendment = existing is not None
        previous: dict[str, Any] | None = None
        if existing is not None:
            previous = {
                "check_in": existing.check_in.isoformat() if existing.check_in else None,
                "check_out": existing.check_out.isoformat() if existing.check_out else None,
                "status": str(existing.status),
                "working_minutes": existing.working_minutes,
            }
            record = existing
        else:
            record = AttendanceRecord(
                company_id=company_id,
                employee_id=employee.id,
                attendance_date=payload.attendance_date,
                created_by=actor.user_id,
            )
            self.uow.attendance.add(record)

        record.shift_id = shift.id if shift else None
        record.check_in = check_in
        record.check_out = check_out
        record.status = override or computation.status
        record.working_minutes = computation.working_minutes
        record.late_minutes = computation.late_minutes
        record.early_leave_minutes = computation.early_leave_minutes
        record.overtime_minutes = computation.overtime_minutes
        record.source = AttendanceSource.MANUAL
        record.notes = payload.notes
        record.updated_by = actor.user_id
        await self.uow.flush()

        self._audit(
            AuditAction.ATTENDANCE_AMENDED if is_amendment else AuditAction.ATTENDANCE_RECORDED,
            "AttendanceRecord",
            record.id,
            company_id,
            actor,
            previous_data=previous,
            new_data={
                "employee_id": str(employee.id),
                "attendance_date": record.attendance_date.isoformat(),
                "check_in": check_in.isoformat() if check_in else None,
                "check_out": check_out.isoformat() if check_out else None,
                "status": str(record.status),
                "working_minutes": record.working_minutes,
            },
        )
        await self.uow.commit()
        await self.uow.refresh(record)

        metrics.attendance_records_total.labels(str(company_id), str(AttendanceSource.MANUAL)).inc()
        return AttendanceRead.model_validate(record)

    # ---------------------------------------------------------------- reads
    async def get(self, company_id: UUID, record_id: UUID) -> AttendanceRead:
        record = await self.uow.attendance.get_scoped(company_id, record_id)
        if record is None:
            raise AttendanceNotFoundError(details={"record_id": str(record_id)})
        return AttendanceRead.model_validate(record)

    async def list_page(
        self,
        company_id: UUID,
        params: PageParams,
        *,
        employee_id: UUID | None = None,
        department_id: UUID | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        status: AttendanceStatus | None = None,
    ) -> Page[AttendanceRead]:
        rows, total = await self.uow.attendance.list_paginated(
            company_id,
            params,
            employee_id=employee_id,
            department_id=department_id,
            start_date=start_date,
            end_date=end_date,
            status=status,
        )
        return Page.build([AttendanceRead.model_validate(r) for r in rows], params, total=total)

    async def summarise(
        self,
        company_id: UUID,
        start_date: date,
        end_date: date,
        *,
        employee_id: UUID | None = None,
        department_id: UUID | None = None,
    ) -> list[AttendanceSummaryRow]:
        rows = await self.uow.attendance.summarise(
            company_id,
            start_date,
            end_date,
            employee_id=employee_id,
            department_id=department_id,
        )
        summaries: dict[UUID, AttendanceSummaryRow] = {}
        for emp_id, status, count, working, overtime, late in rows:
            summary = summaries.setdefault(emp_id, AttendanceSummaryRow(employee_id=emp_id))
            summary.total_working_minutes += working
            summary.total_overtime_minutes += overtime
            summary.total_late_minutes += late
            summary.payable_days += payable_day_fraction(status) * count
            match status:
                case AttendanceStatus.PRESENT:
                    summary.present_days += count
                case AttendanceStatus.ABSENT:
                    summary.absent_days += count
                case AttendanceStatus.LATE:
                    summary.late_days += count
                case AttendanceStatus.HALF_DAY:
                    summary.half_days += count
                case AttendanceStatus.LEAVE:
                    summary.leave_days += count
                case AttendanceStatus.HOLIDAY:
                    summary.holiday_days += count
                case AttendanceStatus.WEEKEND:
                    summary.weekend_days += count

        await self._decorate(company_id, summaries)
        return sorted(summaries.values(), key=lambda s: s.employee_number or "")

    # ------------------------------------------------------------ internals
    async def _decorate(
        self, company_id: UUID, summaries: dict[UUID, AttendanceSummaryRow]
    ) -> None:
        """Attach names in one query rather than one per employee."""
        if not summaries:
            return
        employees = await self.uow.employees.list_payable(
            company_id, as_of=date.max, employee_ids=list(summaries)
        )
        by_id = {e.id: e for e in employees}
        for employee_id, summary in summaries.items():
            employee = by_id.get(employee_id)
            if employee is not None:
                summary.employee_number = employee.employee_number
                summary.full_name = employee.full_name

    async def _ensure_period_open(self, company_id: UUID, day: date, *, what: str) -> None:
        period = await self.uow.payroll_periods.covering(company_id, day)
        if period is not None:
            ensure_period_unlocked_for_source_data(period.status, what=what)

    async def _timezone_for(self, employee: Employee) -> str | None:
        """The location's timezone if it has one, else the company's."""
        if employee.location_id is not None:
            location = await self.uow.locations.get_scoped(
                employee.company_id, employee.location_id
            )
            if location is not None and location.timezone:
                return location.timezone
        company = await self.uow.companies.get(employee.company_id)
        return company.timezone if company else None

    async def _resolve_shift(
        self, employee: Employee, day: date
    ) -> tuple[Shift | None, ShiftDefinition]:
        """The employee's shift on `day`, or a sensible default.

        An unassigned employee is not an error at check-in time — refusing to
        record their arrival would be worse than measuring it against a
        default 9-to-5.
        """
        assignment = await self.uow.shift_assignments.effective_on(employee.id, day)
        if assignment is None:
            return None, _DEFAULT_SHIFT
        shift = assignment.shift
        return shift, ShiftDefinition(
            start_time=shift.start_time,
            end_time=shift.end_time,
            grace_minutes=shift.grace_minutes,
            working_minutes=int(Decimal(str(shift.working_hours)) * 60),
        )

    @staticmethod
    def _scheduled_window(
        day: date, definition: ShiftDefinition, timezone_name: str | None
    ) -> tuple[datetime, datetime]:
        start = combine_shift_datetime(day, definition.start_time, timezone_name=timezone_name)
        end = combine_shift_datetime(
            day,
            definition.end_time,
            next_day=definition.is_overnight,
            timezone_name=timezone_name,
        )
        return start, end

    async def _non_working_status(self, employee: Employee, day: date) -> AttendanceStatus | None:
        """HOLIDAY, WEEKEND or None for a normal working day."""
        holidays = await self.uow.holidays.dates_between(
            employee.company_id, day, day, location_id=employee.location_id
        )
        if day in holidays:
            return AttendanceStatus.HOLIDAY
        if is_weekend(day):
            return AttendanceStatus.WEEKEND
        return None

    async def _require_employee(self, company_id: UUID, employee_id: UUID) -> Employee:
        row = await self.uow.employees.get_scoped(company_id, employee_id)
        if row is None:
            raise EmployeeNotFoundError(details={"employee_id": str(employee_id)})
        return row

    async def _require_shift(self, company_id: UUID, shift_id: UUID) -> Shift:
        row = await self.uow.shifts.get_scoped(company_id, shift_id)
        if row is None:
            raise ShiftNotFoundError(details={"shift_id": str(shift_id)})
        return row

    @staticmethod
    def _shift_read(shift: Shift) -> ShiftRead:
        read = ShiftRead.model_validate(shift)
        return read.model_copy(
            update={"is_overnight": spans_midnight(shift.start_time, shift.end_time)}
        )

    def _audit(
        self,
        action: AuditAction,
        entity_type: str,
        entity_id: UUID,
        company_id: UUID,
        actor: ActorContext,
        *,
        previous_data: dict[str, Any] | None = None,
        new_data: dict[str, Any] | None = None,
    ) -> None:
        self.uow.audit.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            company_id=company_id,
            user_id=actor.user_id,
            previous_data=previous_data,
            new_data=new_data,
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )


_NON_WORKING_STATUSES = frozenset(
    {AttendanceStatus.HOLIDAY, AttendanceStatus.WEEKEND, AttendanceStatus.LEAVE}
)

#: Used when an employee has no shift assignment. Deliberately generous on
#: grace: a default is a guess, and a guess must not cost somebody money.
_DEFAULT_SHIFT = ShiftDefinition(
    start_time=datetime.min.time().replace(hour=9),
    end_time=datetime.min.time().replace(hour=17),
    grace_minutes=settings.attendance_default_grace_minutes,
    working_minutes=8 * 60,
)


__all__ = ["AttendanceService"]

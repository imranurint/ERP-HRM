"""Shift and attendance schemas."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from app.application.common import AppModel, CodeStr, MoneyStr, ORMModel
from app.domain.attendance.enums import AttendanceSource, AttendanceStatus


# --------------------------------------------------------------------- shifts
class ShiftCreate(AppModel):
    shift_code: CodeStr = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    start_time: time
    end_time: time = Field(
        description="An end time at or before the start time means an overnight shift."
    )
    working_hours: MoneyStr = Field(default=Decimal("8"))
    grace_minutes: int = Field(default=0, ge=0, le=240)


class ShiftUpdate(AppModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    start_time: time | None = None
    end_time: time | None = None
    working_hours: MoneyStr | None = None
    grace_minutes: int | None = Field(default=None, ge=0, le=240)
    is_active: bool | None = None


class ShiftRead(ORMModel):
    id: UUID
    company_id: UUID
    shift_code: str
    name: str
    start_time: time
    end_time: time
    grace_minutes: int
    working_hours: MoneyStr
    is_active: bool
    is_overnight: bool = False


class ShiftAssignmentCreate(AppModel):
    employee_id: UUID
    shift_id: UUID
    effective_from: date
    effective_to: date | None = None


class ShiftAssignmentRead(ORMModel):
    id: UUID
    company_id: UUID
    employee_id: UUID
    shift_id: UUID
    effective_from: date
    effective_to: date | None


# ---------------------------------------------------------------- attendance
class CheckInRequest(AppModel):
    """Omit `employee_id` to check yourself in."""

    employee_id: UUID | None = None
    at: datetime | None = Field(
        default=None, description="Defaults to now. Must be timezone-aware."
    )
    source: AttendanceSource = AttendanceSource.API
    notes: str | None = None


class CheckOutRequest(AppModel):
    employee_id: UUID | None = None
    at: datetime | None = None
    notes: str | None = None


class ManualAttendanceRequest(AppModel):
    """Record or amend a day directly. Requires `attendance:write`."""

    employee_id: UUID
    attendance_date: date
    check_in: datetime | None = None
    check_out: datetime | None = None
    #: Override the derived status. Use for HOLIDAY / LEAVE / ABSENT days that
    #: have no check-in pair to derive from.
    status: AttendanceStatus | None = None
    notes: str | None = None


class AttendanceRead(ORMModel):
    id: UUID
    company_id: UUID
    employee_id: UUID
    attendance_date: date
    shift_id: UUID | None
    check_in: datetime | None
    check_out: datetime | None
    status: AttendanceStatus
    working_minutes: int
    overtime_minutes: int
    late_minutes: int
    early_leave_minutes: int
    source: AttendanceSource
    notes: str | None
    created_at: datetime
    updated_at: datetime


class AttendanceSummaryRow(AppModel):
    employee_id: UUID
    employee_number: str | None = None
    full_name: str | None = None
    present_days: int = 0
    absent_days: int = 0
    late_days: int = 0
    half_days: int = 0
    leave_days: int = 0
    holiday_days: int = 0
    weekend_days: int = 0
    total_working_minutes: int = 0
    total_overtime_minutes: int = 0
    total_late_minutes: int = 0
    payable_days: Decimal = Decimal("0")

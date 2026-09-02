"""Attendance arithmetic.

Pure functions over times and minutes. All of it is integer minute maths — no
floats, no timedeltas leaking into storage — so the same check-in pair always
produces the same payable minutes.

Overnight shifts are first-class: a shift whose end time is at or before its
start time (22:00 → 06:00) is understood to finish on the following calendar
day, and the record is still filed under the day the shift *started*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal

from app.core.dates import as_utc, minutes_between, spans_midnight
from app.domain.attendance.enums import AttendanceStatus
from app.domain.attendance.exceptions import InvalidAttendanceWindowError, InvalidShiftError

#: Below this fraction of the shift, a worked day is a half day.
HALF_DAY_THRESHOLD = Decimal("0.5")

MAX_SHIFT_MINUTES = 24 * 60


@dataclass(frozen=True, slots=True)
class ShiftDefinition:
    """The shift an attendance record is measured against."""

    start_time: time
    end_time: time
    grace_minutes: int
    working_minutes: int

    @property
    def is_overnight(self) -> bool:
        return spans_midnight(self.start_time, self.end_time)


@dataclass(frozen=True, slots=True)
class AttendanceComputation:
    """The derived numbers for one attendance record."""

    status: AttendanceStatus
    working_minutes: int
    late_minutes: int
    early_leave_minutes: int
    overtime_minutes: int


def ensure_shift_valid(start_time: time, end_time: time, working_hours: Decimal) -> None:
    """A shift must span a positive, sub-24-hour window."""
    span = minutes_between(start_time, end_time)
    if span <= 0 or span > MAX_SHIFT_MINUTES:
        raise InvalidShiftError(
            "A shift must span between 1 minute and 24 hours.",
            details={"span_minutes": span},
        )
    if working_hours <= 0 or working_hours > 24:
        raise InvalidShiftError(
            "Working hours must be between 0 and 24.",
            details={"working_hours": str(working_hours)},
        )
    declared = int(working_hours * 60)
    if declared > span:
        raise InvalidShiftError(
            "Declared working hours exceed the shift's own span.",
            details={"working_minutes": declared, "span_minutes": span},
        )


def ensure_attendance_window(check_in: datetime, check_out: datetime) -> None:
    if as_utc(check_out) <= as_utc(check_in):
        raise InvalidAttendanceWindowError(
            details={
                "check_in": as_utc(check_in).isoformat(),
                "check_out": as_utc(check_out).isoformat(),
            }
        )


def elapsed_minutes(check_in: datetime, check_out: datetime) -> int:
    """Whole minutes between two instants, floored.

    Flooring is deliberate and consistently applied: partial minutes are never
    rounded up into paid time.
    """
    delta = as_utc(check_out) - as_utc(check_in)
    return int(delta.total_seconds() // 60)


def late_minutes(scheduled_start: datetime, check_in: datetime, grace_minutes: int) -> int:
    """Minutes late, after the grace window.

    Arriving inside grace is exactly zero minutes late, not a small positive
    number — grace exists so a two-minute delay does not become a payroll
    deduction.
    """
    overdue = elapsed_minutes(scheduled_start, check_in)
    if overdue <= grace_minutes:
        return 0
    return overdue


def early_leave_minutes(scheduled_end: datetime, check_out: datetime) -> int:
    shortfall = elapsed_minutes(check_out, scheduled_end)
    return max(0, shortfall)


def overtime_minutes(worked: int, shift_minutes: int, threshold_minutes: int = 0) -> int:
    """Minutes worked beyond the shift, past a configured threshold."""
    excess = worked - shift_minutes
    if excess <= threshold_minutes:
        return 0
    return excess


def compute(
    *,
    shift: ShiftDefinition,
    scheduled_start: datetime,
    scheduled_end: datetime,
    check_in: datetime | None,
    check_out: datetime | None,
    overtime_threshold_minutes: int = 0,
    non_working_day_status: AttendanceStatus | None = None,
) -> AttendanceComputation:
    """Derive every attendance figure for one day.

    `non_working_day_status` (WEEKEND / HOLIDAY / LEAVE) short-circuits: on a
    non-working day the employee owes no hours, so lateness and early-leave
    are meaningless and anything worked is overtime in full.
    """
    if check_in is None:
        return AttendanceComputation(
            status=non_working_day_status or AttendanceStatus.ABSENT,
            working_minutes=0,
            late_minutes=0,
            early_leave_minutes=0,
            overtime_minutes=0,
        )

    if check_out is None:
        # Open record: they are here, but nothing is payable until they leave.
        return AttendanceComputation(
            status=non_working_day_status or _presence_status(0, shift.working_minutes, 0),
            working_minutes=0,
            late_minutes=(
                0
                if non_working_day_status
                else late_minutes(scheduled_start, check_in, shift.grace_minutes)
            ),
            early_leave_minutes=0,
            overtime_minutes=0,
        )

    ensure_attendance_window(check_in, check_out)
    worked = elapsed_minutes(check_in, check_out)

    if non_working_day_status is not None:
        return AttendanceComputation(
            status=non_working_day_status,
            working_minutes=worked,
            late_minutes=0,
            early_leave_minutes=0,
            overtime_minutes=worked,
        )

    late = late_minutes(scheduled_start, check_in, shift.grace_minutes)
    early = early_leave_minutes(scheduled_end, check_out)
    overtime = overtime_minutes(worked, shift.working_minutes, overtime_threshold_minutes)

    return AttendanceComputation(
        status=_presence_status(worked, shift.working_minutes, late),
        working_minutes=worked,
        late_minutes=late,
        early_leave_minutes=early,
        overtime_minutes=overtime,
    )


def _presence_status(worked: int, shift_minutes: int, late: int) -> AttendanceStatus:
    """PRESENT, LATE, HALF_DAY or ABSENT for a day the employee turned up.

    HALF_DAY outranks LATE: somebody who worked two hours of an eight-hour
    shift has a bigger problem than the fifteen minutes they arrived late, and
    payroll proration cares about the former.
    """
    if worked <= 0:
        return AttendanceStatus.ABSENT if late else AttendanceStatus.PRESENT
    if shift_minutes > 0 and Decimal(worked) / Decimal(shift_minutes) < HALF_DAY_THRESHOLD:
        return AttendanceStatus.HALF_DAY
    return AttendanceStatus.LATE if late > 0 else AttendanceStatus.PRESENT


def payable_day_fraction(status: AttendanceStatus) -> Decimal:
    """How much of a day this status is worth when prorating salary.

    Only ABSENT and HALF_DAY reduce pay. Weekends, holidays and leave are
    settled by policy elsewhere — the payroll calculator decides whether a
    given leave type is paid, because only it can see the leave type.
    """
    if status is AttendanceStatus.ABSENT:
        return Decimal("0")
    if status is AttendanceStatus.HALF_DAY:
        return HALF_DAY_THRESHOLD
    return Decimal("1")

"""Turning attendance and leave into the numbers payroll consumes.

Separated from the payroll service because it encodes one significant policy
decision that deserves to be visible and testable on its own:

**Payroll is exception-based.** An employee is assumed present on every
working day in the period, and pay is reduced only by an *explicit* record
saying otherwise — an ABSENT day, a HALF_DAY, or a day of unpaid leave.

The alternative (assume absent unless proven present) is wrong for the common
case: most companies do not record attendance for salaried staff at all, and
under that model a missing biometric feed would zero out everybody's pay.
Under this model a missing feed pays people normally, which is the safe
failure direction — an overpayment is recoverable, an unexplained zero
paycheque is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.core.dates import date_range, is_weekend
from app.modules.attendance.domain.enums import AttendanceStatus
from app.modules.attendance.infrastructure.models import AttendanceRecord
from app.modules.employees.infrastructure.models import Employee
from app.modules.leave.infrastructure.models import LeaveRequest
from app.modules.payroll.domain.calculator import AttendanceSummary

ZERO = Decimal("0.00")
HALF = Decimal("0.5")
ONE = Decimal("1")

#: How much of a day each status deducts from payable days.
_DEDUCTIONS: dict[AttendanceStatus, Decimal] = {
    AttendanceStatus.ABSENT: ONE,
    AttendanceStatus.HALF_DAY: HALF,
}


@dataclass(frozen=True, slots=True)
class PeriodWindow:
    start_date: date
    end_date: date


def working_days(
    window: PeriodWindow,
    *,
    holidays: frozenset[date],
    hire_date: date,
    termination_date: date | None,
) -> tuple[Decimal, list[date]]:
    """Working days in the period the employee was actually employed for.

    A mid-month starter is paid from their start date, not the 1st, and a
    leaver is paid to their last day. Getting this wrong overpays or underpays
    every joiner and leaver, every month.
    """
    days: list[date] = []
    for day in date_range(window.start_date, window.end_date):
        if day < hire_date:
            continue
        if termination_date is not None and day > termination_date:
            continue
        if is_weekend(day) or day in holidays:
            continue
        days.append(day)
    return Decimal(len(days)), days


def resolve(
    employee: Employee,
    window: PeriodWindow,
    *,
    holidays: frozenset[date],
    attendance: Sequence[AttendanceRecord],
    approved_leave: Sequence[LeaveRequest],
) -> AttendanceSummary:
    """Build the `AttendanceSummary` the calculator consumes."""
    total_days, eligible = working_days(
        window,
        holidays=holidays,
        hire_date=employee.hire_date,
        termination_date=employee.termination_date,
    )
    eligible_set = set(eligible)

    by_date = {
        record.attendance_date: record for record in attendance if record.employee_id == employee.id
    }

    unpaid_leave_days = _unpaid_leave_days(employee.id, approved_leave, eligible_set)

    deduction = ZERO
    overtime = 0
    for day in eligible:
        record = by_date.get(day)
        if record is None:
            continue
        overtime += record.overtime_minutes
        deduction += _DEDUCTIONS.get(record.status, ZERO)

    # Overtime worked on a non-working day still counts — the employee was
    # there, and the day being a Saturday is precisely why it is overtime.
    for day, record in by_date.items():
        if day not in eligible_set and window.start_date <= day <= window.end_date:
            overtime += record.overtime_minutes

    payable = total_days - deduction - unpaid_leave_days
    if payable < 0:
        payable = ZERO

    return AttendanceSummary(
        working_days=total_days,
        payable_days=payable,
        unpaid_leave_days=unpaid_leave_days,
        overtime_minutes=overtime,
    )


def _unpaid_leave_days(
    employee_id: UUID,
    approved_leave: Sequence[LeaveRequest],
    eligible: set[date],
) -> Decimal:
    """Working days inside the period spent on *unpaid* leave.

    Paid leave deducts nothing — that is what "paid" means. Only unpaid leave
    reduces the payable day count, and only for days that would otherwise have
    been working days.
    """
    total = ZERO
    for request in approved_leave:
        if request.employee_id != employee_id:
            continue
        if request.leave_type.is_paid:
            continue
        for day in date_range(request.start_date, request.end_date):
            if day in eligible:
                total += ONE
    return total

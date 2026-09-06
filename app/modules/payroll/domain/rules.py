"""Payroll invariants — the Golden Rules, as executable guards.

Every rule in `docs/payroll-rules.md` has a function here, and every payroll
mutation calls one. They are pure: state in, exception or nothing out.

The immutability rules are the reason this module exists. Once payroll is
approved, the numbers behind them have been signed off by a human and, in most
deployments, already sent to a bank and a tax authority. Changing them in place
would break reconciliation with no trace. Corrections are made by a *new*
adjustment or reversal run, never by editing history.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from app.core.dates import overlaps
from app.modules.payroll.domain.enums import (
    CalculationType,
    PayrollPeriodStatus,
    PayrollRunStatus,
    PayslipStatus,
    SalaryComponentType,
    SalaryStructureStatus,
)
from app.modules.payroll.domain.exceptions import (
    PayrollAlreadyApprovedError,
    PayrollAlreadyPaidError,
    PayrollNotApprovedError,
    PayrollNotCalculatedError,
    PayrollPeriodClosedError,
    PayrollPeriodOverlapError,
    PayslipCannotBeModifiedError,
    SalaryComponentInvalidError,
    SalaryStructureInvalidError,
    SalaryStructureOverlapError,
)

MAX_PERCENTAGE = Decimal("100")


# --------------------------------------------------------------------------
# Rule 1 — payroll periods cannot overlap
# --------------------------------------------------------------------------
def ensure_period_dates(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise SalaryStructureInvalidError(
            "Payroll period end date cannot precede its start date.",
            details={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )


def ensure_no_period_overlap(
    start_date: date,
    end_date: date,
    existing: Sequence[tuple[date, date]],
) -> None:
    """Application-side guard, backed by a GiST exclusion constraint.

    Both exist on purpose: the check here produces a good error message, and
    the database constraint is what actually holds under concurrency, where
    two simultaneous inserts would both pass this check.
    """
    ensure_period_dates(start_date, end_date)
    for other_start, other_end in existing:
        if overlaps(start_date, end_date, other_start, other_end):
            raise PayrollPeriodOverlapError(
                details={
                    "requested_start": start_date.isoformat(),
                    "requested_end": end_date.isoformat(),
                    "conflicting_start": other_start.isoformat(),
                    "conflicting_end": other_end.isoformat(),
                }
            )


def ensure_period_open(status: PayrollPeriodStatus) -> None:
    if not status.accepts_runs:
        raise PayrollPeriodClosedError(
            f"This payroll period is {status} and accepts no further runs.",
            details={"status": str(status)},
        )


def ensure_period_accepts_adjustment(status: PayrollPeriodStatus) -> None:
    """An *adjustment* run may enter an already-approved or paid period.

    Golden Rule 7 says a post-approval correction is a new run rather than an
    edit — so refusing every run into an APPROVED period would make the rule
    impossible to follow. A CLOSED period is genuinely closed: reopen it
    deliberately before correcting anything inside it.
    """
    if status is PayrollPeriodStatus.CLOSED:
        raise PayrollPeriodClosedError(
            "This payroll period is CLOSED. Reopen it before posting an adjustment run.",
            details={"status": str(status)},
        )


def ensure_pay_date(start_date: date, pay_date: date | None) -> None:
    """A pay date before the period even starts is always a typo."""
    if pay_date is not None and pay_date < start_date:
        raise SalaryStructureInvalidError(
            "Pay date cannot precede the start of the payroll period.",
            details={
                "pay_date": pay_date.isoformat(),
                "start_date": start_date.isoformat(),
            },
        )


# --------------------------------------------------------------------------
# Rules 2 & 3 — approved and paid payroll are immutable
# --------------------------------------------------------------------------
def ensure_run_mutable(status: PayrollRunStatus) -> None:
    """Guard every recalculation, deletion or edit of a run's payslips."""
    if status is PayrollRunStatus.PAID:
        raise PayrollAlreadyPaidError(details={"status": str(status)})
    if status is PayrollRunStatus.APPROVED:
        raise PayrollAlreadyApprovedError(details={"status": str(status)})
    if status is PayrollRunStatus.PROCESSING:
        from app.modules.payroll.domain.exceptions import PayrollRunInProgressError

        raise PayrollRunInProgressError(details={"status": str(status)})


def ensure_can_approve(status: PayrollRunStatus) -> None:
    if status.is_approved:
        raise PayrollAlreadyApprovedError(details={"status": str(status)})
    if status is not PayrollRunStatus.COMPLETED:
        raise PayrollNotCalculatedError(
            f"A {status} run cannot be approved; calculate it first.",
            details={"status": str(status)},
        )


def ensure_can_pay(status: PayrollRunStatus) -> None:
    if status is PayrollRunStatus.PAID:
        raise PayrollAlreadyPaidError(details={"status": str(status)})
    if status is not PayrollRunStatus.APPROVED:
        raise PayrollNotApprovedError(
            f"A {status} run cannot be paid; approve it first.",
            details={"status": str(status)},
        )


def ensure_payslip_mutable(status: PayslipStatus) -> None:
    if not status.is_mutable:
        raise PayslipCannotBeModifiedError(
            f"A {status} payslip cannot be modified.",
            details={"status": str(status)},
        )


def ensure_payslip_voidable(status: PayslipStatus) -> None:
    """Voiding a *paid* payslip is refused.

    Money has left the building; the correction is a negative adjustment run,
    which leaves both the original and the correction visible.
    """
    if status is PayslipStatus.PAID:
        raise PayrollAlreadyPaidError(
            "A paid payslip cannot be voided; process an adjustment run instead.",
            details={"status": str(status)},
        )
    if status is PayslipStatus.VOID:
        raise PayslipCannotBeModifiedError(
            "This payslip is already void.",
            details={"status": str(status)},
        )


# --------------------------------------------------------------------------
# Rule 8 — attendance and leave feeding processed payroll are frozen
# --------------------------------------------------------------------------
def ensure_period_unlocked_for_source_data(status: PayrollPeriodStatus, *, what: str) -> None:
    """Reject edits to the inputs of a payroll that has already been signed off."""
    if status.is_locked:
        from app.modules.attendance.domain.exceptions import AttendanceLockedError

        raise AttendanceLockedError(
            f"{what} falls in a payroll period that is {status} and can no longer change.",
            details={"payroll_period_status": str(status), "entity": what},
        )


# --------------------------------------------------------------------------
# Salary structures and components
# --------------------------------------------------------------------------
def ensure_structure_dates(effective_from: date, effective_to: date | None) -> None:
    if effective_to is not None and effective_to < effective_from:
        raise SalaryStructureInvalidError(
            "Salary structure end date cannot precede its start date.",
            details={
                "effective_from": effective_from.isoformat(),
                "effective_to": effective_to.isoformat(),
            },
        )


def ensure_base_salary(base_salary: Decimal) -> None:
    if base_salary < 0:
        raise SalaryStructureInvalidError(
            "Base salary cannot be negative.",
            details={"base_salary": str(base_salary)},
        )


def ensure_no_structure_overlap(
    effective_from: date,
    effective_to: date | None,
    existing: Sequence[tuple[date, date | None]],
) -> None:
    """An employee has at most one salary structure in effect on any date.

    Also enforced by a GiST exclusion constraint. Without it, "the salary on
    2026-03-01" would have two answers and payroll would pick one arbitrarily.
    """
    ensure_structure_dates(effective_from, effective_to)
    for other_from, other_to in existing:
        if overlaps(effective_from, effective_to, other_from, other_to):
            raise SalaryStructureOverlapError(
                details={
                    "requested_from": effective_from.isoformat(),
                    "requested_to": effective_to.isoformat() if effective_to else None,
                    "conflicting_from": other_from.isoformat(),
                    "conflicting_to": other_to.isoformat() if other_to else None,
                }
            )


def ensure_structure_payable(status: SalaryStructureStatus) -> None:
    if not status.is_payable:
        raise SalaryStructureInvalidError(
            f"A {status} salary structure cannot be used for payroll.",
            details={"status": str(status)},
        )


def ensure_component_configuration(
    *,
    component_code: str,
    calculation_type: CalculationType,
    component_type: SalaryComponentType,
    amount: Decimal | None,
    percentage: Decimal | None,
    formula_key: str | None,
) -> None:
    """A component must carry exactly the inputs its calculation type needs."""
    match calculation_type:
        case CalculationType.FIXED:
            if amount is None:
                raise SalaryComponentInvalidError(
                    f"{component_code} is FIXED and requires an amount.",
                    details={"component_code": component_code},
                )
            if amount < 0:
                raise SalaryComponentInvalidError(
                    f"{component_code} has a negative amount; use a "
                    f"{SalaryComponentType.DEDUCTION} component instead.",
                    details={"component_code": component_code, "amount": str(amount)},
                )

        case CalculationType.PERCENTAGE:
            if percentage is None:
                raise SalaryComponentInvalidError(
                    f"{component_code} is PERCENTAGE and requires a percentage.",
                    details={"component_code": component_code},
                )
            if not 0 <= percentage <= MAX_PERCENTAGE:
                raise SalaryComponentInvalidError(
                    f"{component_code} has a percentage outside 0–100.",
                    details={"component_code": component_code, "percentage": str(percentage)},
                )

        case CalculationType.FORMULA:
            if not formula_key:
                raise SalaryComponentInvalidError(
                    f"{component_code} is FORMULA and requires a formula_key.",
                    details={"component_code": component_code},
                )
            # Fail at configuration time rather than mid-payroll-run.
            from app.modules.payroll.domain.formulas import resolve

            resolve(formula_key)

    if component_type is SalaryComponentType.EMPLOYER_CONTRIBUTION and percentage is not None:
        # An employer contribution sits outside the employee's pay, so a
        # percentage of GROSS_EARNINGS is meaningful but must still be a real
        # percentage — guarded above. Nothing further to check.
        return

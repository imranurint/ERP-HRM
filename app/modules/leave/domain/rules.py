"""Leave invariants and balance arithmetic.

Pure functions. The important property here is that
`closing_balance == opening_balance + earned - used + adjustment` holds
*by construction* everywhere, so the projection in `leave_balances` and a
replay of `leave_transactions` can never disagree.

Leave days are `Decimal`, not `int` or `float`: half days are real, and
`0.5 + 0.5 + 0.5` must be exactly `1.5`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.dates import date_range, is_weekend
from app.modules.leave.domain.enums import LeaveRequestStatus, LeaveTransactionType
from app.modules.leave.domain.exceptions import (
    InvalidLeaveDatesError,
    LeaveAlreadyDecidedError,
    LeaveBalanceInsufficientError,
    LeaveExceedsAnnualLimitError,
    LeaveNotCancellableError,
    LeaveNotPendingError,
    LeaveOverlapError,
    SelfApprovalError,
)

ZERO_DAYS = Decimal("0.00")
FULL_DAY = Decimal("1.00")
HALF_DAY = Decimal("0.50")
DAY_QUANT = Decimal("0.01")

#: A single request may not span more than this. Longer absences are modelled
#: as sabbatical/unpaid arrangements, not as one enormous leave row.
MAX_LEAVE_SPAN_DAYS = 365


@dataclass(frozen=True, slots=True)
class LeaveBalanceSnapshot:
    """One employee's balance for one leave type in one year."""

    opening_balance: Decimal
    earned: Decimal
    used: Decimal
    adjustment: Decimal

    @property
    def closing_balance(self) -> Decimal:
        return quantize_days(self.opening_balance + self.earned - self.used + self.adjustment)

    def available(self, pending_days: Decimal = ZERO_DAYS) -> Decimal:
        """Days the employee can still book.

        `pending_days` subtracts leave that is requested but not yet approved,
        so two requests submitted the same morning cannot both be approved
        against the same remaining day.
        """
        return quantize_days(self.closing_balance - pending_days)


def quantize_days(value: Decimal) -> Decimal:
    """Leave days carry two decimals — enough for halves and quarters."""
    return value.quantize(DAY_QUANT)


# --------------------------------------------------------------------------
# Date maths
# --------------------------------------------------------------------------
def ensure_leave_dates(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise InvalidLeaveDatesError(
            "Leave end date cannot precede its start date.",
            details={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )
    if (end_date - start_date).days + 1 > MAX_LEAVE_SPAN_DAYS:
        raise InvalidLeaveDatesError(
            "A single leave request cannot span more than a year.",
            details={"max_days": MAX_LEAVE_SPAN_DAYS},
        )


def count_leave_days(
    start_date: date,
    end_date: date,
    *,
    holidays: Iterable[date] = (),
    weekend_days: frozenset[int] = frozenset({5, 6}),
    exclude_non_working: bool = True,
    half_days: Iterable[date] = (),
) -> Decimal:
    """Working days in a leave span.

    Weekends and public holidays are excluded by default: an employee who
    books Friday to Monday over a normal weekend has used two days of
    entitlement, not four. Set `exclude_non_working=False` for leave types
    that consume calendar days (some unpaid and maternity schemes do).
    """
    ensure_leave_dates(start_date, end_date)

    holiday_set = set(holidays)
    half_day_set = set(half_days)

    total = ZERO_DAYS
    for day in date_range(start_date, end_date):
        if exclude_non_working and (is_weekend(day, weekend_days) or day in holiday_set):
            continue
        total += HALF_DAY if day in half_day_set else FULL_DAY
    return quantize_days(total)


def ensure_no_overlap(
    start_date: date,
    end_date: date,
    existing: Sequence[tuple[date, date]],
) -> None:
    """Reject a request that collides with leave the employee already holds.

    `existing` carries only ranges in a status that still occupies the
    calendar (DRAFT, PENDING, APPROVED). Rejected and cancelled leave is not
    passed in, because it no longer blocks anything.
    """
    for other_start, other_end in existing:
        if start_date <= other_end and other_start <= end_date:
            raise LeaveOverlapError(
                details={
                    "requested_start": start_date.isoformat(),
                    "requested_end": end_date.isoformat(),
                    "conflicting_start": other_start.isoformat(),
                    "conflicting_end": other_end.isoformat(),
                }
            )


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------
def ensure_can_submit(status: LeaveRequestStatus) -> None:
    if status is not LeaveRequestStatus.DRAFT:
        raise LeaveAlreadyDecidedError(
            f"A {status} request cannot be submitted.",
            details={"status": str(status)},
        )


def ensure_can_decide(status: LeaveRequestStatus) -> None:
    """Approval and rejection both require a PENDING request."""
    if status is not LeaveRequestStatus.PENDING:
        raise LeaveNotPendingError(
            f"A {status} request cannot be approved or rejected.",
            details={"status": str(status)},
        )


def ensure_not_self_approval(requester_employee_id: object, approver_employee_id: object) -> None:
    """Separation of duties, enforced regardless of role.

    An HR Manager who is also an employee still cannot sign off their own
    holiday — a permission grant is not supposed to be a loophole.
    """
    if (
        approver_employee_id is not None
        and requester_employee_id is not None
        and approver_employee_id == requester_employee_id
    ):
        raise SelfApprovalError(details={"employee_id": str(requester_employee_id)})


def ensure_can_cancel(status: LeaveRequestStatus, start_date: date, *, today: date) -> None:
    """Cancellation is allowed until the leave has started.

    Approved leave that has already begun is history: cancelling it would
    silently return days the employee has actually taken. Correct that with an
    explicit balance adjustment instead, which leaves an audit trail.
    """
    if status in (LeaveRequestStatus.CANCELLED, LeaveRequestStatus.REJECTED):
        raise LeaveNotCancellableError(
            f"This request is already {status}.",
            details={"status": str(status)},
        )
    if status is LeaveRequestStatus.APPROVED and start_date <= today:
        raise LeaveNotCancellableError(
            "Approved leave that has already started cannot be cancelled; "
            "record a balance adjustment instead.",
            details={"start_date": start_date.isoformat(), "today": today.isoformat()},
        )


# --------------------------------------------------------------------------
# Balance checks
# --------------------------------------------------------------------------
def ensure_sufficient_balance(
    *,
    requested_days: Decimal,
    snapshot: LeaveBalanceSnapshot,
    pending_days: Decimal = ZERO_DAYS,
    leave_code: str,
    allow_negative: bool = False,
) -> None:
    """Reject a request the employee cannot cover.

    `allow_negative` exists for leave types a company deliberately lets run
    into deficit (advance annual leave, for instance). It is a per-leave-type
    policy flag, never a global escape hatch.
    """
    if allow_negative:
        return
    available = snapshot.available(pending_days)
    if requested_days > available:
        raise LeaveBalanceInsufficientError.shortfall(
            requested=quantize_days(requested_days),
            available=available,
            leave_code=leave_code,
        )


def ensure_within_annual_limit(
    *,
    requested_days: Decimal,
    used_this_year: Decimal,
    annual_limit: Decimal | None,
    leave_code: str,
) -> None:
    """Some leave types cap total usage independently of accrued balance."""
    if annual_limit is None:
        return
    projected = used_this_year + requested_days
    if projected > annual_limit:
        raise LeaveExceedsAnnualLimitError(
            f"This request would take {leave_code} usage to {quantize_days(projected)} days, "
            f"above the annual limit of {quantize_days(annual_limit)}.",
            details={
                "leave_code": leave_code,
                "annual_limit": str(quantize_days(annual_limit)),
                "used_this_year": str(quantize_days(used_this_year)),
                "requested_days": str(quantize_days(requested_days)),
            },
        )


def carry_forward_days(
    *,
    closing_balance: Decimal,
    carry_forward_allowed: bool,
    carry_forward_limit: Decimal | None,
) -> Decimal:
    """How much of a year's closing balance survives into the next one."""
    if not carry_forward_allowed or closing_balance <= 0:
        return ZERO_DAYS
    if carry_forward_limit is None:
        return quantize_days(closing_balance)
    return quantize_days(min(closing_balance, carry_forward_limit))


def apply_transaction(
    snapshot: LeaveBalanceSnapshot,
    transaction_type: LeaveTransactionType,
    days: Decimal,
) -> LeaveBalanceSnapshot:
    """Fold one movement into a snapshot.

    This is the single definition of what each transaction type means, shared
    by the live posting path and by the from-scratch rebuild — which is why
    the two cannot drift.
    """
    opening = snapshot.opening_balance
    earned = snapshot.earned
    used = snapshot.used
    adjustment = snapshot.adjustment

    match transaction_type:
        case LeaveTransactionType.CARRY_FORWARD:
            opening += days
        case LeaveTransactionType.ENTITLEMENT | LeaveTransactionType.ACCRUAL:
            earned += days
        case LeaveTransactionType.CONSUMPTION:
            used += days
        case LeaveTransactionType.CANCELLATION:
            used -= days
        case LeaveTransactionType.ADJUSTMENT | LeaveTransactionType.EXPIRY:
            adjustment += days

    return LeaveBalanceSnapshot(
        opening_balance=quantize_days(opening),
        earned=quantize_days(earned),
        used=quantize_days(used),
        adjustment=quantize_days(adjustment),
    )


def replay(transactions: Iterable[tuple[LeaveTransactionType, Decimal]]) -> LeaveBalanceSnapshot:
    """Rebuild a balance from its movements. Used by the rebuild script."""
    snapshot = LeaveBalanceSnapshot(ZERO_DAYS, ZERO_DAYS, ZERO_DAYS, ZERO_DAYS)
    for transaction_type, days in transactions:
        snapshot = apply_transaction(snapshot, transaction_type, days)
    return snapshot

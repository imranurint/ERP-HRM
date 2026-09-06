"""Leave enumerations."""

from __future__ import annotations

from enum import StrEnum


class LeaveRequestStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

    @property
    def is_open(self) -> bool:
        """Still awaiting a decision — and still holding balance."""
        return self in (LeaveRequestStatus.DRAFT, LeaveRequestStatus.PENDING)

    @property
    def is_final(self) -> bool:
        return self in (
            LeaveRequestStatus.APPROVED,
            LeaveRequestStatus.REJECTED,
            LeaveRequestStatus.CANCELLED,
        )

    @property
    def consumes_balance(self) -> bool:
        """Only approved leave consumes entitlement.

        Pending requests are *checked* against the balance but do not spend
        it; the reservation is enforced by counting open requests, not by
        writing a transaction that would then need unwinding on rejection.
        """
        return self is LeaveRequestStatus.APPROVED


class LeaveTransactionType(StrEnum):
    """The append-only movements `leave_balances` is derived from.

    The balance table is a projection. Every figure in it must be reproducible
    by replaying these transactions, which is what
    `python -m scripts.rebuild_leave_balances` verifies.
    """

    ENTITLEMENT = "ENTITLEMENT"
    CARRY_FORWARD = "CARRY_FORWARD"
    ACCRUAL = "ACCRUAL"
    CONSUMPTION = "CONSUMPTION"
    CANCELLATION = "CANCELLATION"
    ADJUSTMENT = "ADJUSTMENT"
    EXPIRY = "EXPIRY"

    @property
    def column(self) -> str:
        """Which `leave_balances` column this movement accumulates into."""
        return _TRANSACTION_COLUMNS[self]

    @property
    def increases_available(self) -> bool:
        return self is not LeaveTransactionType.CONSUMPTION


_TRANSACTION_COLUMNS: dict[LeaveTransactionType, str] = {
    LeaveTransactionType.ENTITLEMENT: "earned",
    LeaveTransactionType.ACCRUAL: "earned",
    LeaveTransactionType.CARRY_FORWARD: "opening_balance",
    LeaveTransactionType.CONSUMPTION: "used",
    # A cancellation gives days back by reducing `used`, not by inflating
    # `earned`: the employee never earned those days twice.
    LeaveTransactionType.CANCELLATION: "used",
    LeaveTransactionType.ADJUSTMENT: "adjustment",
    LeaveTransactionType.EXPIRY: "adjustment",
}


class LeaveDayUnit(StrEnum):
    """Granularity of a single day within a leave request."""

    FULL_DAY = "FULL_DAY"
    HALF_DAY = "HALF_DAY"

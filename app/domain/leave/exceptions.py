"""Leave domain errors."""

from __future__ import annotations

from decimal import Decimal

from app.core.exceptions import ConflictError, NotFoundError, ValidationError


class LeaveTypeNotFoundError(NotFoundError):
    code = "LEAVE_TYPE_NOT_FOUND"
    message = "The requested leave type does not exist."


class LeavePolicyNotFoundError(NotFoundError):
    code = "LEAVE_POLICY_NOT_FOUND"
    message = "The requested leave policy does not exist."


class LeaveRequestNotFoundError(NotFoundError):
    code = "LEAVE_REQUEST_NOT_FOUND"
    message = "The requested leave request does not exist."


class InvalidLeaveDatesError(ValidationError):
    code = "INVALID_LEAVE_DATES"
    message = "The leave date range is not valid."


class LeaveBalanceInsufficientError(ConflictError):
    code = "LEAVE_BALANCE_INSUFFICIENT"
    message = "The employee does not have enough leave balance."

    @classmethod
    def shortfall(
        cls, *, requested: Decimal, available: Decimal, leave_code: str
    ) -> LeaveBalanceInsufficientError:
        return cls(
            f"Requesting {requested} days of {leave_code} but only {available} are available.",
            details={
                "leave_code": leave_code,
                "requested_days": str(requested),
                "available_days": str(available),
                "shortfall_days": str(requested - available),
            },
        )


class LeaveAlreadyApprovedError(ConflictError):
    code = "LEAVE_ALREADY_APPROVED"
    message = "This leave request has already been approved."


class LeaveAlreadyDecidedError(ConflictError):
    code = "LEAVE_ALREADY_DECIDED"
    message = "This leave request has already been decided."


class LeaveNotPendingError(ConflictError):
    code = "LEAVE_NOT_PENDING"
    message = "Only a pending leave request can be approved or rejected."


class LeaveOverlapError(ConflictError):
    code = "LEAVE_OVERLAP"
    message = "This employee already has leave booked over part of that range."


class LeaveNotCancellableError(ConflictError):
    code = "LEAVE_NOT_CANCELLABLE"
    message = "This leave request can no longer be cancelled."


class SelfApprovalError(ConflictError):
    """Nobody approves their own leave, whatever permissions they hold."""

    code = "SELF_APPROVAL_FORBIDDEN"
    message = "You cannot approve your own leave request."


class LeaveExceedsAnnualLimitError(ConflictError):
    code = "LEAVE_EXCEEDS_ANNUAL_LIMIT"
    message = "This request would exceed the annual limit for that leave type."

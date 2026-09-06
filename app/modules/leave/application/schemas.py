"""Leave schemas."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from app.modules.leave.domain.enums import LeaveRequestStatus, LeaveTransactionType
from app.shared.schemas import AppModel, CodeStr, DaysStr, ORMModel


# ---------------------------------------------------------------- leave types
class LeaveTypeCreate(AppModel):
    leave_code: CodeStr = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    is_paid: bool = True
    annual_limit: DaysStr | None = None
    carry_forward_limit: DaysStr | None = None
    requires_approval: bool = True
    excludes_non_working_days: bool = Field(
        default=True,
        description="Exclude weekends and holidays inside the span from the day count.",
    )
    allows_negative_balance: bool = False


class LeaveTypeUpdate(AppModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    is_paid: bool | None = None
    annual_limit: DaysStr | None = None
    carry_forward_limit: DaysStr | None = None
    requires_approval: bool | None = None
    excludes_non_working_days: bool | None = None
    allows_negative_balance: bool | None = None
    is_active: bool | None = None


class LeaveTypeRead(ORMModel):
    id: UUID
    company_id: UUID
    leave_code: str
    name: str
    description: str | None
    is_paid: bool
    annual_limit: DaysStr | None
    carry_forward_limit: DaysStr | None
    requires_approval: bool
    excludes_non_working_days: bool
    allows_negative_balance: bool
    is_active: bool


# ------------------------------------------------------------------- policies
class LeavePolicyRuleInput(AppModel):
    leave_type_id: UUID
    annual_entitlement: DaysStr
    carry_forward_allowed: bool = False
    carry_forward_limit: DaysStr | None = None
    minimum_service_days: int = Field(default=0, ge=0)


class LeavePolicyCreate(AppModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    rules: list[LeavePolicyRuleInput] = Field(default_factory=list)


class LeavePolicyRuleRead(ORMModel):
    id: UUID
    leave_type_id: UUID
    annual_entitlement: DaysStr
    carry_forward_allowed: bool
    carry_forward_limit: DaysStr | None
    minimum_service_days: int


class LeavePolicyRead(ORMModel):
    id: UUID
    company_id: UUID
    name: str
    description: str | None
    is_active: bool
    rules: list[LeavePolicyRuleRead] = Field(default_factory=list)


class AssignPolicyRequest(AppModel):
    employee_id: UUID
    leave_policy_id: UUID
    effective_from: date
    effective_to: date | None = None
    #: Grant the policy's annual entitlement for this year immediately.
    grant_entitlement: bool = True


class EmployeeLeavePolicyRead(ORMModel):
    id: UUID
    company_id: UUID
    employee_id: UUID
    leave_policy_id: UUID
    effective_from: date
    effective_to: date | None


# ------------------------------------------------------------------- requests
class LeaveRequestCreate(AppModel):
    leave_type_id: UUID
    start_date: date
    end_date: date
    #: Omit to check yourself out. Requires `leave:manage` to book for others.
    employee_id: UUID | None = None
    reason: str | None = None
    half_days: list[date] = Field(
        default_factory=list,
        description="Dates within the span taken as half days.",
    )
    #: Submit for approval immediately rather than saving a draft.
    submit: bool = True


class LeaveDecisionRequest(AppModel):
    reason: str | None = None


class LeaveRejectRequest(AppModel):
    rejection_reason: str = Field(min_length=1)


class LeaveRequestRead(ORMModel):
    id: UUID
    company_id: UUID
    employee_id: UUID
    leave_type_id: UUID
    leave_code: str | None = None
    start_date: date
    end_date: date
    total_days: DaysStr
    reason: str | None
    status: LeaveRequestStatus
    requested_at: datetime | None
    approved_at: datetime | None
    approved_by: UUID | None
    rejected_at: datetime | None
    rejected_by: UUID | None
    rejection_reason: str | None
    cancelled_at: datetime | None
    version: int
    created_at: datetime


# ------------------------------------------------------------------- balances
class LeaveBalanceRead(AppModel):
    company_id: UUID
    employee_id: UUID
    leave_type_id: UUID
    leave_code: str | None = None
    leave_name: str | None = None
    year: int
    opening_balance: DaysStr
    earned: DaysStr
    used: DaysStr
    adjustment: DaysStr
    closing_balance: DaysStr
    pending_days: DaysStr
    available: DaysStr


class BalanceAdjustmentRequest(AppModel):
    employee_id: UUID
    leave_type_id: UUID
    year: int = Field(ge=1900, le=9999)
    days: DaysStr = Field(description="Positive to grant, negative to withdraw.")
    notes: str = Field(min_length=1, description="Adjustments always require a reason.")


class LeaveTransactionRead(ORMModel):
    id: UUID
    employee_id: UUID
    leave_type_id: UUID
    year: int
    transaction_type: LeaveTransactionType
    days: DaysStr
    source_type: str | None
    source_id: UUID | None
    notes: str | None
    created_at: datetime

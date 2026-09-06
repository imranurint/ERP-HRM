"""Leave types, policies, requests, transactions and balances.

`leave_balances` is a **projection**. `leave_transactions` is the append-only
truth it is derived from, and
`python -m scripts.rebuild_leave_balances --all --verify` proves the two
agree. That is the same relationship the Accounting module has between its
journals and its ledger balance table, for the same reason: a derived table
you cannot rebuild is a derived table you cannot trust.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.modules.leave.domain.enums import LeaveRequestStatus, LeaveTransactionType
from app.shared.database.base import (
    ActorTrackingMixin,
    Base,
    CreatedAtMixin,
    DaysNumeric,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    days_column,
)
from app.shared.database.types import status_enum


class LeaveType(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    __tablename__ = "leave_types"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    leave_code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: Unpaid leave still consumes calendar days and still shows on the
    #: payslip — as unworked days that reduce pay.
    is_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    annual_limit: Mapped[Decimal | None] = mapped_column(DaysNumeric)
    carry_forward_limit: Mapped[Decimal | None] = mapped_column(DaysNumeric)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Whether weekends and holidays inside the span consume entitlement.
    #: Annual leave normally excludes them; some statutory leave does not.
    excludes_non_working_days: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Allows the balance to go negative — advance leave, by policy.
    allows_negative_balance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("company_id", "leave_code", name="uq_leave_types_company_id_leave_code"),
        CheckConstraint(
            "annual_limit IS NULL OR annual_limit >= 0", name="annual_limit_non_negative"
        ),
        CheckConstraint(
            "carry_forward_limit IS NULL OR carry_forward_limit >= 0",
            name="carry_forward_limit_non_negative",
        ),
        CheckConstraint("length(trim(leave_code)) > 0", name="leave_code_not_blank"),
        Index("ix_leave_types_company_active", "company_id", "is_active"),
    )


class LeavePolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """A named bundle of per-leave-type entitlement rules."""

    __tablename__ = "leave_policies"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    rules: Mapped[list[LeavePolicyRule]] = relationship(
        back_populates="policy", lazy="selectin", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_leave_policies_company_id_name"),
        Index("ix_leave_policies_company_active", "company_id", "is_active"),
    )


class LeavePolicyRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "leave_policy_rules"

    policy_id: Mapped[UUID] = mapped_column(
        ForeignKey("leave_policies.id", ondelete="CASCADE"), nullable=False
    )
    leave_type_id: Mapped[UUID] = mapped_column(
        ForeignKey("leave_types.id", ondelete="RESTRICT"), nullable=False
    )
    annual_entitlement: Mapped[Decimal] = days_column()
    carry_forward_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    carry_forward_limit: Mapped[Decimal | None] = mapped_column(DaysNumeric)
    #: Days of service before this leave type unlocks. Probationers often
    #: cannot take annual leave in their first three months.
    minimum_service_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    policy: Mapped[LeavePolicy] = relationship(back_populates="rules")
    leave_type: Mapped[LeaveType] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint(
            "policy_id", "leave_type_id", name="uq_leave_policy_rules_policy_id_leave_type_id"
        ),
        CheckConstraint("annual_entitlement >= 0", name="annual_entitlement_non_negative"),
        CheckConstraint(
            "carry_forward_limit IS NULL OR carry_forward_limit >= 0",
            name="rule_carry_forward_limit_non_negative",
        ),
        CheckConstraint("minimum_service_days >= 0", name="minimum_service_days_non_negative"),
        # A carry-forward limit is meaningless if carry-forward is off.
        CheckConstraint(
            "carry_forward_allowed OR carry_forward_limit IS NULL",
            name="carry_forward_limit_requires_allowed",
        ),
    )


class EmployeeLeavePolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """Which policy applies to an employee, over a date range.

    Non-overlapping per employee (GiST exclusion constraint): an employee has
    exactly one leave policy on any given day.
    """

    __tablename__ = "employee_leave_policies"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    leave_policy_id: Mapped[UUID] = mapped_column(
        ForeignKey("leave_policies.id", ondelete="RESTRICT"), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)

    policy: Mapped[LeavePolicy] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="employee_policy_date_order",
        ),
        Index("ix_employee_leave_policies_employee", "employee_id", "effective_from"),
        Index("ix_employee_leave_policies_company", "company_id"),
    )


class LeaveRequest(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    __tablename__ = "leave_requests"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    leave_type_id: Mapped[UUID] = mapped_column(
        ForeignKey("leave_types.id", ondelete="RESTRICT"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_days: Mapped[Decimal] = days_column()
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[LeaveRequestStatus] = mapped_column(
        status_enum(LeaveRequestStatus, "leave_request_status", 16),
        nullable=False,
        default=LeaveRequestStatus.DRAFT,
    )

    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    #: Optimistic lock — two approvers clicking at once must not both win.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    leave_type: Mapped[LeaveType] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="leave_date_order"),
        CheckConstraint("total_days > 0", name="total_days_positive"),
        # Decision metadata must match the decision. An APPROVED row with no
        # approver, or a PENDING row carrying a rejection, is corrupt.
        CheckConstraint(
            "(status <> 'APPROVED') OR (approved_at IS NOT NULL)",
            name="approved_has_timestamp",
        ),
        CheckConstraint(
            "(status <> 'REJECTED') OR (rejected_at IS NOT NULL)",
            name="rejected_has_timestamp",
        ),
        CheckConstraint(
            "(status <> 'CANCELLED') OR (cancelled_at IS NOT NULL)",
            name="cancelled_has_timestamp",
        ),
        Index("ix_leave_requests_employee_status", "employee_id", "status"),
        Index("ix_leave_requests_company_status", "company_id", "status"),
        Index("ix_leave_requests_dates", "employee_id", "start_date", "end_date"),
        Index("ix_leave_requests_type", "leave_type_id"),
    )


class LeaveTransaction(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Append-only leave movement. Never updated, never deleted.

    Every row in `leave_balances` is reproducible by replaying these through
    `app.modules.leave.domain.rules.replay`.
    """

    __tablename__ = "leave_transactions"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    leave_type_id: Mapped[UUID] = mapped_column(
        ForeignKey("leave_types.id", ondelete="RESTRICT"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_type: Mapped[LeaveTransactionType] = mapped_column(
        status_enum(LeaveTransactionType, "leave_transaction_type", 24), nullable=False
    )
    days: Mapped[Decimal] = days_column()
    #: What caused this movement — a leave request, a policy grant, a manual
    #: adjustment. Mirrors the Accounting module's source_type/source_document
    #: provenance pattern.
    source_type: Mapped[str | None] = mapped_column(String(32))
    source_id: Mapped[UUID | None] = mapped_column()
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID | None] = mapped_column()

    __table_args__ = (
        CheckConstraint("days >= 0", name="transaction_days_non_negative"),
        CheckConstraint("year BETWEEN 1900 AND 9999", name="transaction_year_range"),
        Index(
            "ix_leave_transactions_balance_key",
            "company_id",
            "employee_id",
            "leave_type_id",
            "year",
        ),
        Index("ix_leave_transactions_source", "source_type", "source_id"),
    )


class LeaveBalance(Base, TimestampMixin):
    """Derived per (company, employee, leave type, year).

    The composite natural key *is* the primary key — there is nothing a
    surrogate id would add, and it makes the atomic upsert's conflict target
    obvious.

    `closing_balance` is recomputed from the post-increment totals on every
    upsert, never read-modify-written, so concurrent approvals cannot lose an
    update.
    """

    __tablename__ = "leave_balances"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), primary_key=True
    )
    leave_type_id: Mapped[UUID] = mapped_column(
        ForeignKey("leave_types.id", ondelete="RESTRICT"), primary_key=True
    )
    year: Mapped[int] = mapped_column(Integer, primary_key=True)

    opening_balance: Mapped[Decimal] = days_column()
    earned: Mapped[Decimal] = days_column()
    used: Mapped[Decimal] = days_column()
    adjustment: Mapped[Decimal] = days_column()
    closing_balance: Mapped[Decimal] = days_column()

    __table_args__ = (
        CheckConstraint("year BETWEEN 1900 AND 9999", name="balance_year_range"),
        CheckConstraint("earned >= 0 AND used >= 0", name="balance_components_non_negative"),
        # The projection's own invariant, checked by the database on every
        # write. If this ever fires, the upsert arithmetic is wrong.
        CheckConstraint(
            "closing_balance = opening_balance + earned - used + adjustment",
            name="closing_balance_derivation",
        ),
        Index("ix_leave_balances_employee_year", "employee_id", "year"),
        Index("ix_leave_balances_company_year", "company_id", "year"),
    )

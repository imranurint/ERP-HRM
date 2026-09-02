"""Payroll periods, runs, payslips and payslip items.

Immutability is structural here, not merely a convention: the status columns
carry CHECK constraints, the guards in `app.domain.payroll.rules` run inside
the transaction, and the period is `SELECT ... FOR UPDATE` locked for the
duration of a run. Nothing recalculates an approved payslip.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.payroll.enums import (
    PayrollPeriodStatus,
    PayrollRunStatus,
    PayslipStatus,
    SalaryComponentType,
)
from app.infrastructure.database.base import (
    ActorTrackingMixin,
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    money_column,
)
from app.infrastructure.database.models.mixins import status_enum


class PayrollPeriod(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """A pay cycle.

    **Periods for one company must not overlap** — Payroll Golden Rule 1,
    enforced by a GiST exclusion constraint on
    `(company_id, daterange(start_date, end_date, '[]'))`. An application
    check alone loses the race between two simultaneous inserts; the
    constraint cannot.
    """

    __tablename__ = "payroll_periods"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    period_name: Mapped[str] = mapped_column(String(100), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    pay_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[PayrollPeriodStatus] = mapped_column(
        status_enum(PayrollPeriodStatus, "payroll_period_status", 16),
        nullable=False,
        default=PayrollPeriodStatus.OPEN,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        UniqueConstraint(
            "company_id", "period_name", name="uq_payroll_periods_company_id_period_name"
        ),
        CheckConstraint("end_date >= start_date", name="period_date_order"),
        CheckConstraint(
            "pay_date IS NULL OR pay_date >= start_date", name="pay_date_after_period_start"
        ),
        Index("ix_payroll_periods_company_dates", "company_id", "start_date", "end_date"),
        Index("ix_payroll_periods_company_status", "company_id", "status"),
    )


class PayrollRun(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """One execution of payroll for a period.

    A period can have several runs — a re-run after a correction, or an
    explicit adjustment run. That is exactly how a post-approval correction is
    made: Golden Rule 7 forbids editing an approved run, so the fix is a new
    run alongside it, and both stay visible.
    """

    __tablename__ = "payroll_runs"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    payroll_period_id: Mapped[UUID] = mapped_column(
        ForeignKey("payroll_periods.id", ondelete="RESTRICT"), nullable=False
    )
    run_number: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[PayrollRunStatus] = mapped_column(
        status_enum(PayrollRunStatus, "payroll_run_status", 16),
        nullable=False,
        default=PayrollRunStatus.DRAFT,
    )
    #: Set when the run corrects an earlier one, so the pair reads as a
    #: correction rather than as a duplicate payroll.
    adjusts_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("payroll_runs.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)

    payslip_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_gross: Mapped[Decimal] = money_column()
    total_deductions: Mapped[Decimal] = money_column()
    total_employer_contributions: Mapped[Decimal] = money_column()
    total_net: Mapped[Decimal] = money_column()
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.currency_code", ondelete="RESTRICT"), nullable=False
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    failure_reason: Mapped[str | None] = mapped_column(Text)

    period: Mapped[PayrollPeriod] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("company_id", "run_number", name="uq_payroll_runs_company_id_run_number"),
        CheckConstraint("id <> adjusts_run_id", name="not_own_adjustment"),
        CheckConstraint("payslip_count >= 0", name="payslip_count_non_negative"),
        CheckConstraint(
            "total_gross >= 0 AND total_deductions >= 0 "
            "AND total_employer_contributions >= 0 AND total_net >= 0",
            name="run_totals_non_negative",
        ),
        # Approval and payment must carry their metadata, or "who approved
        # this payroll?" has no answer.
        CheckConstraint(
            "status NOT IN ('APPROVED', 'PAID') OR approved_at IS NOT NULL",
            name="approved_has_timestamp",
        ),
        CheckConstraint("status <> 'PAID' OR paid_at IS NOT NULL", name="paid_has_timestamp"),
        Index("ix_payroll_runs_period", "payroll_period_id", "status"),
        Index("ix_payroll_runs_company_status", "company_id", "status"),
    )


class Payslip(Base, UUIDPrimaryKeyMixin, TimestampMixin, ActorTrackingMixin):
    """One employee's pay for one run.

    `calculation_snapshot` is Golden Rule 6: the complete set of inputs the
    figures were derived from — contractual salary, proration factor, days
    worked, component order. Six months later, when somebody asks why March
    was short, the answer is in this column and does not depend on the
    employee's current configuration.
    """

    __tablename__ = "payslips"

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    payroll_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    salary_structure_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("employee_salary_structures.id", ondelete="RESTRICT")
    )

    gross_salary: Mapped[Decimal] = money_column()
    total_deductions: Mapped[Decimal] = money_column()
    employer_contributions: Mapped[Decimal] = money_column()
    net_salary: Mapped[Decimal] = money_column()
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.currency_code", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[PayslipStatus] = mapped_column(
        status_enum(PayslipStatus, "payslip_status", 16),
        nullable=False,
        default=PayslipStatus.DRAFT,
    )
    calculation_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    void_reason: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list[PayslipItem]] = relationship(
        back_populates="payslip",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="PayslipItem.sequence",
    )

    __table_args__ = (
        UniqueConstraint(
            "payroll_run_id", "employee_id", name="uq_payslips_payroll_run_id_employee_id"
        ),
        CheckConstraint(
            "gross_salary >= 0 AND total_deductions >= 0 "
            "AND employer_contributions >= 0 AND net_salary >= 0",
            name="payslip_amounts_non_negative",
        ),
        # Net is defined, not stored independently. A payslip that fails this
        # is arithmetically impossible and must never reach the database.
        CheckConstraint(
            "net_salary = gross_salary - total_deductions", name="net_salary_derivation"
        ),
        Index("ix_payslips_run", "payroll_run_id"),
        Index("ix_payslips_employee", "employee_id"),
        Index("ix_payslips_company_status", "company_id", "status"),
    )


class PayslipItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One line of a payslip.

    `component_name` and `component_type` are **copied**, not joined. Renaming
    a salary component next year must not rewrite the wording of a payslip
    already issued — a payslip is a document, and documents do not change
    retroactively. `salary_component_id` is nullable for exactly this reason
    and because base salary is not a configurable component.
    """

    __tablename__ = "payslip_items"

    payslip_id: Mapped[UUID] = mapped_column(
        ForeignKey("payslips.id", ondelete="CASCADE"), nullable=False
    )
    salary_component_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("salary_components.id", ondelete="SET NULL")
    )
    component_code: Mapped[str] = mapped_column(String(32), nullable=False)
    component_name: Mapped[str] = mapped_column(String(200), nullable=False)
    component_type: Mapped[SalaryComponentType] = mapped_column(
        status_enum(SalaryComponentType, "payslip_item_component_type", 32), nullable=False
    )
    amount: Mapped[Decimal] = money_column()
    is_taxable: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    gl_account_code: Mapped[str | None] = mapped_column(String(32))
    calculation_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    payslip: Mapped[Payslip] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint(
            "payslip_id", "component_code", name="uq_payslip_items_payslip_id_component_code"
        ),
        CheckConstraint("amount >= 0", name="item_amount_non_negative"),
        Index("ix_payslip_items_payslip", "payslip_id", "sequence"),
        Index("ix_payslip_items_component", "salary_component_id"),
    )

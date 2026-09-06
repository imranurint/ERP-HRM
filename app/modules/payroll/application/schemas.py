"""Payroll schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.modules.payroll.domain.enums import (
    PayrollPeriodStatus,
    PayrollRunStatus,
    PayslipStatus,
    SalaryComponentType,
)
from app.shared.schemas import AppModel, CurrencyCode, DaysStr, MoneyStr, ORMModel


# ---------------------------------------------------------------- periods
class PayrollPeriodCreate(AppModel):
    period_name: str = Field(min_length=1, max_length=100)
    start_date: date
    end_date: date
    pay_date: date | None = None


class PayrollPeriodRead(ORMModel):
    id: UUID
    company_id: UUID
    period_name: str
    start_date: date
    end_date: date
    pay_date: date | None
    status: PayrollPeriodStatus
    closed_at: datetime | None
    closed_by: UUID | None
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------- runs
class PayrollRunCreate(AppModel):
    payroll_period_id: UUID
    notes: str | None = None
    #: Set when this run corrects an already-approved one. Golden Rule 7
    #: forbids editing approved payroll, so a correction is a *new* run.
    adjusts_run_id: UUID | None = None


class PayrollCalculateRequest(AppModel):
    """Restrict the run to specific employees. Omit for everybody payable."""

    employee_ids: list[UUID] = Field(default_factory=list)


class PayrollRunRead(ORMModel):
    id: UUID
    company_id: UUID
    payroll_period_id: UUID
    run_number: str
    status: PayrollRunStatus
    adjusts_run_id: UUID | None
    notes: str | None
    payslip_count: int
    total_gross: MoneyStr
    total_deductions: MoneyStr
    total_employer_contributions: MoneyStr
    total_net: MoneyStr
    currency_code: str
    started_at: datetime | None
    completed_at: datetime | None
    approved_at: datetime | None
    approved_by: UUID | None
    paid_at: datetime | None
    paid_by: UUID | None
    failure_reason: str | None
    created_at: datetime


# --------------------------------------------------------------- payslips
class PayslipItemRead(ORMModel):
    id: UUID
    salary_component_id: UUID | None
    component_code: str
    component_name: str
    component_type: SalaryComponentType
    amount: MoneyStr
    is_taxable: bool
    sequence: int
    gl_account_code: str | None
    calculation_metadata: dict[str, Any] | None


class PayslipRead(ORMModel):
    id: UUID
    company_id: UUID
    payroll_run_id: UUID
    employee_id: UUID
    salary_structure_id: UUID | None
    gross_salary: MoneyStr
    total_deductions: MoneyStr
    employer_contributions: MoneyStr
    net_salary: MoneyStr
    currency_code: str
    status: PayslipStatus
    calculation_snapshot: dict[str, Any]
    void_reason: str | None
    items: list[PayslipItemRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class VoidPayslipRequest(AppModel):
    reason: str = Field(min_length=1)


class PayrollJournalLine(AppModel):
    """One aggregated line of the journal handed to the Accounting module.

    Debits and credits balance by construction:
    `gross + employer == deductions + net + employer_payable`.
    """

    gl_account_code: str | None
    side: str = Field(pattern="^(DEBIT|CREDIT)$")
    amount: MoneyStr
    component_code: str
    description: str


class PayrollApprovalPreview(AppModel):
    """What approving this run will hand to Accounting. Read-only, no side effects."""

    run_id: UUID
    run_number: str
    currency_code: CurrencyCode
    total_debits: MoneyStr
    total_credits: MoneyStr
    balanced: bool
    lines: list[PayrollJournalLine]


class PayrollSummaryRow(AppModel):
    employee_id: UUID
    employee_number: str | None = None
    full_name: str | None = None
    gross_salary: MoneyStr
    total_deductions: MoneyStr
    employer_contributions: MoneyStr
    net_salary: MoneyStr
    payable_days: DaysStr
    status: PayslipStatus

"""Domain events.

Events are written to `outbox_events` inside the same database transaction as
the HR change that produced them (transactional outbox). A separate relay —
deliberately outside that transaction — publishes them to whatever bus the
rest of the ERP subscribes to.

`PayrollApproved` is the important one: it carries a journal-ready payload so
the Accounting module can build a `CreateJournalCommand` without knowing
anything about employees. See `docs/architecture.md`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base event. `event_type` is the contract subscribers bind to."""

    event_type: ClassVar[str] = "DomainEvent"
    aggregate_type: ClassVar[str] = "Unknown"

    aggregate_id: UUID
    company_id: UUID

    def payload(self) -> dict[str, Any]:
        data = dict(asdict(self))
        return dict(_jsonable(data))


# --------------------------------------------------------------------------
# Employee lifecycle
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EmployeeHired(DomainEvent):
    event_type: ClassVar[str] = "EmployeeHired"
    aggregate_type: ClassVar[str] = "Employee"

    employee_number: str = ""
    full_name: str = ""
    hire_date: date | None = None
    department_id: UUID | None = None
    designation_id: UUID | None = None
    created_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class EmployeeStatusChanged(DomainEvent):
    event_type: ClassVar[str] = "EmployeeStatusChanged"
    aggregate_type: ClassVar[str] = "Employee"

    employee_number: str = ""
    previous_status: str = ""
    new_status: str = ""
    effective_date: date | None = None
    reason: str | None = None
    changed_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class EmployeeTransferred(DomainEvent):
    event_type: ClassVar[str] = "EmployeeTransferred"
    aggregate_type: ClassVar[str] = "Employee"

    employee_number: str = ""
    previous_department_id: UUID | None = None
    new_department_id: UUID | None = None
    previous_location_id: UUID | None = None
    new_location_id: UUID | None = None
    effective_date: date | None = None
    changed_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class SalaryChanged(DomainEvent):
    event_type: ClassVar[str] = "SalaryChanged"
    aggregate_type: ClassVar[str] = "EmployeeSalaryStructure"

    employee_id: UUID | None = None
    employee_number: str = ""
    previous_base_salary: Decimal | None = None
    new_base_salary: Decimal = Decimal("0.0000")
    currency_code: str = ""
    effective_from: date | None = None
    changed_by: UUID | None = None


# --------------------------------------------------------------------------
# Leave
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LeaveRequested(DomainEvent):
    event_type: ClassVar[str] = "LeaveRequested"
    aggregate_type: ClassVar[str] = "LeaveRequest"

    employee_id: UUID | None = None
    leave_type_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    total_days: Decimal = Decimal("0.0000")
    requested_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class LeaveApproved(DomainEvent):
    event_type: ClassVar[str] = "LeaveApproved"
    aggregate_type: ClassVar[str] = "LeaveRequest"

    employee_id: UUID | None = None
    leave_type_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    total_days: Decimal = Decimal("0.0000")
    is_paid: bool = True
    approved_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class LeaveRejected(DomainEvent):
    event_type: ClassVar[str] = "LeaveRejected"
    aggregate_type: ClassVar[str] = "LeaveRequest"

    employee_id: UUID | None = None
    rejection_reason: str | None = None
    rejected_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class LeaveCancelled(DomainEvent):
    event_type: ClassVar[str] = "LeaveCancelled"
    aggregate_type: ClassVar[str] = "LeaveRequest"

    employee_id: UUID | None = None
    total_days: Decimal = Decimal("0.0000")
    was_approved: bool = False
    cancelled_by: UUID | None = None


# --------------------------------------------------------------------------
# Payroll
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PayrollCalculated(DomainEvent):
    event_type: ClassVar[str] = "PayrollCalculated"
    aggregate_type: ClassVar[str] = "PayrollRun"

    run_number: str = ""
    payroll_period_id: UUID | None = None
    payslip_count: int = 0
    total_gross: Decimal = Decimal("0.0000")
    total_net: Decimal = Decimal("0.0000")
    currency_code: str = ""
    calculated_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class PayrollApproved(DomainEvent):
    """Approved payroll, in a shape the Accounting module can post directly.

    `journal_lines` are pre-aggregated per (gl_account_code, side), which is
    everything `CreateJournalCommand` needs. HRM does not know what a journal
    entry is; it knows which GL account code each salary component maps to,
    because that mapping is HR configuration.
    """

    event_type: ClassVar[str] = "PayrollApproved"
    aggregate_type: ClassVar[str] = "PayrollRun"

    run_number: str = ""
    payroll_period_id: UUID | None = None
    period_name: str = ""
    period_start: date | None = None
    period_end: date | None = None
    pay_date: date | None = None
    currency_code: str = ""
    payslip_count: int = 0
    total_gross: Decimal = Decimal("0.0000")
    total_deductions: Decimal = Decimal("0.0000")
    total_employer_contributions: Decimal = Decimal("0.0000")
    total_net: Decimal = Decimal("0.0000")
    approved_by: UUID | None = None
    journal_lines: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PayrollPaid(DomainEvent):
    event_type: ClassVar[str] = "PayrollPaid"
    aggregate_type: ClassVar[str] = "PayrollRun"

    run_number: str = ""
    payroll_period_id: UUID | None = None
    pay_date: date | None = None
    total_net: Decimal = Decimal("0.0000")
    currency_code: str = ""
    paid_by: UUID | None = None

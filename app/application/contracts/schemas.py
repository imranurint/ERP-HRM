"""Employment contract schemas."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from app.application.common import AppModel, ORMModel
from app.domain.employees.enums import ContractStatus, ContractType


class ContractCreate(AppModel):
    employee_id: UUID
    contract_type: ContractType
    start_date: date
    end_date: date | None = Field(
        default=None,
        description="Required for every type except PERMANENT — without it a "
        "fixed-term contract can never expire.",
    )
    probation_end_date: date | None = None
    working_hours_per_week: int | None = Field(default=None, ge=1, le=168)
    #: Omit to have one allocated atomically, e.g. `CON-2026-000001`.
    contract_number: str | None = Field(default=None, max_length=32)
    notes: str | None = None
    #: Activate immediately. The partial unique index rejects a second active
    #: contract for the same employee.
    activate: bool = False


class ContractUpdate(AppModel):
    end_date: date | None = None
    probation_end_date: date | None = None
    working_hours_per_week: int | None = Field(default=None, ge=1, le=168)
    notes: str | None = None


class ContractRead(ORMModel):
    id: UUID
    company_id: UUID
    employee_id: UUID
    contract_number: str
    contract_type: ContractType
    start_date: date
    end_date: date | None
    probation_end_date: date | None
    working_hours_per_week: int | None
    status: ContractStatus
    notes: str | None
    created_at: datetime
    updated_at: datetime


class ContractTerminateRequest(AppModel):
    end_date: date
    reason: str | None = None

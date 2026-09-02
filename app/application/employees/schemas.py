"""Employee and lifecycle schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import EmailStr, Field

from app.application.common import AppModel, ORMModel
from app.domain.employees.enums import EmploymentStatus, Gender, LifecycleEventType


class EmployeeCreate(AppModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    work_email: EmailStr
    hire_date: date
    #: Omit to have one allocated atomically, e.g. `EMP-2026-000001`.
    employee_number: str | None = Field(default=None, max_length=32)
    personal_email: EmailStr | None = None
    work_phone: str | None = Field(default=None, max_length=50)
    personal_phone: str | None = Field(default=None, max_length=50)
    date_of_birth: date | None = None
    gender: Gender | None = None
    nationality: str | None = Field(default=None, max_length=100)
    department_id: UUID | None = None
    designation_id: UUID | None = None
    manager_id: UUID | None = None
    location_id: UUID | None = None
    employment_status: EmploymentStatus = EmploymentStatus.PROBATION


class EmployeeUpdate(AppModel):
    """Contact and identity fields only.

    Department, designation and manager changes go through `/transfer` and
    `/promote` so they leave a lifecycle event behind. Employment status goes
    through the dedicated lifecycle routes so the transition graph is checked.
    """

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    work_email: EmailStr | None = None
    personal_email: EmailStr | None = None
    work_phone: str | None = Field(default=None, max_length=50)
    personal_phone: str | None = Field(default=None, max_length=50)
    date_of_birth: date | None = None
    gender: Gender | None = None
    nationality: str | None = Field(default=None, max_length=100)
    location_id: UUID | None = None
    #: Optimistic lock. Send the version you read; a mismatch is a 409.
    version: int | None = None


class EmployeeRead(ORMModel):
    id: UUID
    company_id: UUID
    employee_number: str
    user_id: UUID | None
    first_name: str
    last_name: str
    full_name: str
    work_email: str
    personal_email: str | None
    work_phone: str | None
    personal_phone: str | None
    date_of_birth: date | None
    gender: Gender | None
    nationality: str | None
    hire_date: date
    termination_date: date | None
    employment_status: EmploymentStatus
    department_id: UUID | None
    designation_id: UUID | None
    manager_id: UUID | None
    location_id: UUID | None
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


class TerminateRequest(AppModel):
    termination_date: date
    reason: str | None = None
    #: RESIGNED when the employee left, TERMINATED when the company ended it.
    #: They are legally and statistically different and reports separate them.
    resigned: bool = False


class ActivateRequest(AppModel):
    effective_date: date | None = None
    reason: str | None = None


class SuspendRequest(AppModel):
    effective_date: date | None = None
    reason: str = Field(min_length=1)


class TransferRequest(AppModel):
    department_id: UUID | None = None
    location_id: UUID | None = None
    manager_id: UUID | None = None
    effective_date: date | None = None
    reason: str | None = None


class PromoteRequest(AppModel):
    designation_id: UUID
    effective_date: date | None = None
    reason: str | None = None
    #: A promotion often comes with a manager change.
    manager_id: UUID | None = None


class LifecycleEventRead(ORMModel):
    id: UUID
    company_id: UUID
    employee_id: UUID
    event_type: LifecycleEventType
    effective_date: date
    previous_data: dict[str, Any]
    new_data: dict[str, Any]
    reason: str | None
    created_by: UUID | None
    created_at: datetime

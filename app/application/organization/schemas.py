"""Department, designation, location and holiday schemas."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from app.application.common import AppModel, CodeStr, ORMModel


# --------------------------------------------------------------- departments
class DepartmentCreate(AppModel):
    department_code: CodeStr = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    parent_department_id: UUID | None = None
    manager_employee_id: UUID | None = None


class DepartmentUpdate(AppModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    parent_department_id: UUID | None = None
    manager_employee_id: UUID | None = None
    is_active: bool | None = None


class DepartmentRead(ORMModel):
    id: UUID
    company_id: UUID
    department_code: str
    name: str
    description: str | None
    parent_department_id: UUID | None
    manager_employee_id: UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# -------------------------------------------------------------- designations
class DesignationCreate(AppModel):
    designation_code: CodeStr = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    grade_level: int | None = Field(default=None, ge=1, le=100)


class DesignationUpdate(AppModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    grade_level: int | None = Field(default=None, ge=1, le=100)
    is_active: bool | None = None


class DesignationRead(ORMModel):
    id: UUID
    company_id: UUID
    designation_code: str
    name: str
    description: str | None
    grade_level: int | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ locations
class LocationCreate(AppModel):
    location_code: CodeStr = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    address: str | None = None
    timezone: str | None = Field(
        default=None,
        max_length=64,
        description="Overrides the company timezone for attendance at this site.",
    )


class LocationUpdate(AppModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = None
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None


class LocationRead(ORMModel):
    id: UUID
    company_id: UUID
    location_code: str
    name: str
    address: str | None
    timezone: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ holidays
class HolidayCreate(AppModel):
    holiday_date: date
    name: str = Field(min_length=1, max_length=200)
    location_id: UUID | None = Field(default=None, description="Omit for a company-wide holiday.")


class HolidayRead(ORMModel):
    id: UUID
    company_id: UUID
    location_id: UUID | None
    holiday_date: date
    name: str
    is_active: bool

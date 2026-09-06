"""Organization request/response models: companies, currencies, departments,
designations, locations and holidays."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import EmailStr, Field

from app.shared.schemas import AppModel, CodeStr, CurrencyCode, ORMModel


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


# ----------------------------------------------------------------- companies
class CompanyCreate(AppModel):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str = Field(min_length=1, max_length=255)
    base_currency_code: CurrencyCode = Field(min_length=3, max_length=3)
    registration_number: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = None
    timezone: str = Field(default="UTC", max_length=64)


class CompanyUpdate(AppModel):
    """`base_currency_code` is deliberately absent — see the route docstring."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, min_length=1, max_length=255)
    registration_number: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = None
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None


class CompanyRead(ORMModel):
    id: UUID
    name: str
    legal_name: str
    registration_number: str | None
    email: str | None
    phone: str | None
    address: str | None
    timezone: str
    base_currency_code: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CurrencyRead(ORMModel):
    currency_code: str
    currency_name: str
    decimal_places: int
    is_active: bool

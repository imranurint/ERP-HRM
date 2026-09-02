"""Company schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import EmailStr, Field

from app.application.common import AppModel, CurrencyCode, ORMModel


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

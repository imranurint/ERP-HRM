"""Authentication schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import EmailStr, Field

from app.shared.schemas import AppModel, ORMModel


class RegisterRequest(AppModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)
    roles: list[str] = Field(
        default_factory=list, description="Role names to assign, e.g. ['HR Officer']."
    )
    employee_id: UUID | None = Field(
        default=None,
        description="Link this login to an employee record, enabling self-service.",
    )


class LoginRequest(AppModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(AppModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(AppModel):
    refresh_token: str | None = None
    all_sessions: bool = False


class TokenPair(AppModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: datetime
    expires_in: int


class UserRead(ORMModel):
    id: UUID
    email: str
    full_name: str | None
    is_active: bool
    is_superuser: bool
    last_login_at: datetime | None = None
    created_at: datetime
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    employee_id: UUID | None = None
    employee_number: str | None = None
    company_id: UUID | None = None

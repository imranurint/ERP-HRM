"""Identity domain entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.domain.identity.permissions import Permission


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """The caller behind the current request.

    Built once per request from the access token (with permissions re-read
    from the database), then passed down into application services so they can
    authorise and audit without touching HTTP.

    `employee_id` links a login to a person. It is what makes self-service
    routes safe: an Employee-role caller may read *their* leave balance and
    nobody else's, and that check needs the link, not a permission.
    """

    id: UUID
    email: str
    full_name: str | None
    is_active: bool
    is_superuser: bool
    employee_id: UUID | None = None
    permissions: frozenset[Permission] = field(default_factory=frozenset)

    def has_permission(self, permission: Permission) -> bool:
        return self.is_superuser or permission in self.permissions

    def has_all(self, *permissions: Permission) -> bool:
        return all(self.has_permission(p) for p in permissions)

    def has_any(self, *permissions: Permission) -> bool:
        return any(self.has_permission(p) for p in permissions)

    def is_self(self, employee_id: UUID) -> bool:
        return self.employee_id is not None and self.employee_id == employee_id


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    id: UUID
    name: str
    description: str | None
    permissions: frozenset[Permission]

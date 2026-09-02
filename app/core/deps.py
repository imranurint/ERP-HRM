"""FastAPI dependencies.

Routes declare what they need — a Unit of Work, the current user, a
permission — and get it injected. This is where authentication and
authorisation live; no route re-implements either.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.context import ActorContext
from app.core.exceptions import UnauthorizedError
from app.core.security import decode_access_token
from app.domain.identity.entities import AuthenticatedUser
from app.domain.identity.exceptions import (
    InactiveUserError,
    NotYourRecordError,
    PermissionDeniedError,
)
from app.domain.identity.permissions import Permission
from app.infrastructure.database.session import get_session_factory
from app.infrastructure.database.unit_of_work import UnitOfWork

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="JWT")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
async def get_session() -> AsyncIterator[AsyncSession]:
    """One session per request, closed when the response is finished."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_uow(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UnitOfWork:
    return UnitOfWork(session)


UowDep = Annotated[UnitOfWork, Depends(get_uow)]


# --------------------------------------------------------------------------
# Request context
# --------------------------------------------------------------------------
def get_request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else None


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
async def get_current_user(
    request: Request,
    uow: UowDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> AuthenticatedUser:
    """Resolve the caller from the bearer token.

    Permissions are re-read from the database rather than trusted from the
    token: revoking a role must take effect immediately, not when the access
    token happens to expire. The same applies to the employee link, so
    unlinking a login from an employee record instantly stops self-service.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Missing bearer token.")

    payload = decode_access_token(credentials.credentials)
    try:
        user_id = UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Malformed access token.", code="INVALID_TOKEN") from exc

    user = await uow.users.get_with_permissions(user_id)
    if user is None:
        raise UnauthorizedError("User no longer exists.", code="INVALID_TOKEN")
    if not user.is_active:
        raise InactiveUserError(details={"user_id": str(user_id)})

    codes = await uow.users.permission_codes(user_id)
    permissions = frozenset(
        Permission(code) for code in codes if code in Permission._value2member_map_
    )
    employee = await uow.employees.get_by_user_id(user_id)

    authenticated = AuthenticatedUser(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        is_superuser=user.is_superuser,
        employee_id=employee.id if employee else None,
        permissions=permissions,
    )
    request.state.user_id = str(user.id)
    return authenticated


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


def require(
    *permissions: Permission,
) -> Callable[[AuthenticatedUser], Coroutine[Any, Any, AuthenticatedUser]]:
    """Dependency factory enforcing permissions (not role names).

    @router.post("", dependencies=[Depends(require(Permission.PAYROLL_APPROVE))])
    """

    async def dependency(user: CurrentUser) -> AuthenticatedUser:
        for permission in permissions:
            if not user.has_permission(permission):
                raise PermissionDeniedError.for_permission(str(permission))
        return user

    return dependency


def require_any(
    *permissions: Permission,
) -> Callable[[AuthenticatedUser], Coroutine[Any, Any, AuthenticatedUser]]:
    async def dependency(user: CurrentUser) -> AuthenticatedUser:
        if not user.has_any(*permissions):
            raise PermissionDeniedError(
                "This action requires one of: " + ", ".join(str(p) for p in permissions),
                details={"required_any": [str(p) for p in permissions]},
            )
        return user

    return dependency


def ensure_self_or_permission(
    user: AuthenticatedUser, employee_id: UUID, permission: Permission
) -> None:
    """Allow a caller at their own record, or anywhere with the permission.

    This is the self-service boundary. An Employee-role caller holds
    `leave:read` but may only point it at themselves; an HR Officer holds
    `employees:read` and may point it anywhere in the company.
    """
    if user.is_self(employee_id):
        return
    if user.has_permission(permission):
        return
    raise NotYourRecordError(
        f"You may only access your own record without the '{permission}' permission.",
        details={"employee_id": str(employee_id), "required_permission": str(permission)},
    )


# --------------------------------------------------------------------------
# Actor context for auditing
# --------------------------------------------------------------------------
def get_actor(request: Request, user: CurrentUser) -> ActorContext:
    return ActorContext(
        user_id=user.id,
        employee_id=user.employee_id,
        request_id=get_request_id(request),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def get_anonymous_actor(request: Request) -> ActorContext:
    """Actor context for unauthenticated routes (login, refresh)."""
    return ActorContext(
        user_id=None,
        request_id=get_request_id(request),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


ActorDep = Annotated[ActorContext, Depends(get_actor)]
AnonymousActorDep = Annotated[ActorContext, Depends(get_anonymous_actor)]

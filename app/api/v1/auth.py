"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.application.auth.schemas import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserRead,
)
from app.application.auth.service import AuthService
from app.core.deps import ActorDep, AnonymousActorDep, CurrentUser, UowDep, require
from app.domain.identity.permissions import Permission

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user",
    dependencies=[Depends(require(Permission.USERS_MANAGE))],
)
async def register(payload: RegisterRequest, uow: UowDep, actor: ActorDep) -> UserRead:
    """Registration is an administrative action, not self-service.

    Anyone who can create a user could grant themselves payroll approval
    rights, so this requires `users:manage`. Bootstrap the first administrator
    with `python -m app.scripts.create_admin`.
    """
    return await AuthService(uow).register(payload, actor)


@router.post("/login", response_model=TokenPair, summary="Exchange credentials for tokens")
async def login(payload: LoginRequest, uow: UowDep, actor: AnonymousActorDep) -> TokenPair:
    return await AuthService(uow).login(payload, actor)


@router.post("/refresh", response_model=TokenPair, summary="Rotate a refresh token")
async def refresh(payload: RefreshRequest, uow: UowDep, actor: AnonymousActorDep) -> TokenPair:
    """The presented token is revoked and replaced.

    Presenting an already-revoked token is treated as a compromise signal and
    revokes every session for that user.
    """
    return await AuthService(uow).refresh(payload.refresh_token, actor)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current or all sessions",
)
async def logout(
    uow: UowDep,
    user: CurrentUser,
    actor: ActorDep,
    payload: LogoutRequest | None = None,
) -> None:
    request = payload or LogoutRequest()
    await AuthService(uow).logout(
        request.refresh_token, user.id, all_sessions=request.all_sessions, actor=actor
    )


@router.get("/me", response_model=UserRead, summary="The authenticated user")
async def me(uow: UowDep, user: CurrentUser) -> UserRead:
    return await AuthService(uow).me(user.id)

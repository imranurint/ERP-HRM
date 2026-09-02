"""Authentication and user management."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.application.auth.schemas import (
    LoginRequest,
    RegisterRequest,
    TokenPair,
    UserRead,
)
from app.application.context import ActorContext
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    refresh_token_expiry,
    verify_password,
)
from app.domain.identity.exceptions import (
    EmailAlreadyRegisteredError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    RoleNotFoundError,
)
from app.domain.shared.enums import AuditAction
from app.infrastructure.database.models.identity import RefreshToken, User
from app.infrastructure.database.unit_of_work import UnitOfWork

logger = get_logger(__name__)


class AuthService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    # -------------------------------------------------------------- register
    async def register(self, payload: RegisterRequest, actor: ActorContext) -> UserRead:
        email = payload.email.strip().lower()
        if await self.uow.users.get_by_email(email) is not None:
            raise EmailAlreadyRegisteredError(details={"email": email})

        user = User(
            email=email,
            hashed_password=hash_password(payload.password),
            full_name=payload.full_name,
            is_active=True,
            is_superuser=False,
        )
        self.uow.users.add(user)

        for role_name in payload.roles:
            role = await self.uow.roles.get_by_name(role_name)
            if role is None:
                raise RoleNotFoundError(
                    f"Role {role_name!r} does not exist.", details={"role": role_name}
                )
            user.roles.append(role)

        await self.uow.flush()

        company_id: UUID | None = None
        if payload.employee_id is not None:
            employee = await self.uow.employees.get(payload.employee_id)
            if employee is None:
                raise NotFoundError(
                    "Employee not found.", details={"employee_id": str(payload.employee_id)}
                )
            # The partial unique index on employees.user_id is what actually
            # prevents two logins sharing one employee record.
            employee.user_id = user.id
            company_id = employee.company_id

        self.uow.audit.record(
            action=AuditAction.USER_CREATED,
            entity_type="User",
            entity_id=user.id,
            company_id=company_id,
            user_id=actor.user_id,
            new_data={
                "email": email,
                "roles": payload.roles,
                "employee_id": str(payload.employee_id) if payload.employee_id else None,
            },
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()

        loaded = await self.uow.users.get_with_permissions(user.id)
        assert loaded is not None
        return await self._to_read(loaded)

    # ----------------------------------------------------------------- login
    async def login(self, payload: LoginRequest, actor: ActorContext) -> TokenPair:
        user = await self.uow.users.get_by_email(payload.email)

        # Verify against a dummy hash when the user is unknown so the response
        # time does not reveal which emails exist.
        if user is None:
            verify_password(payload.password, _DUMMY_HASH)
            raise InvalidCredentialsError()

        if not verify_password(payload.password, user.hashed_password):
            raise InvalidCredentialsError()
        if not user.is_active:
            raise InactiveUserError(details={"user_id": str(user.id)})

        if password_needs_rehash(user.hashed_password):
            user.hashed_password = hash_password(payload.password)

        user.last_login_at = datetime.now(UTC)

        employee = await self.uow.employees.get_by_user_id(user.id)

        self.uow.audit.record(
            action=AuditAction.USER_LOGIN,
            entity_type="User",
            entity_id=user.id,
            company_id=employee.company_id if employee else None,
            user_id=user.id,
            new_data={"email": user.email},
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )

        tokens = await self._issue_tokens(user, actor)
        await self.uow.commit()
        return tokens

    # --------------------------------------------------------------- refresh
    async def refresh(self, raw_token: str, actor: ActorContext) -> TokenPair:
        """Rotate a refresh token.

        The presented token is revoked and replaced. Presenting an already
        revoked token is treated as a compromise signal and revokes every
        session for that user.
        """
        token_hash = hash_refresh_token(raw_token)
        stored = await self.uow.refresh_tokens.get_active_by_hash(token_hash)

        if stored is None:
            replayed = await self.uow.refresh_tokens.get_by_hash(token_hash)
            if replayed is not None and replayed.revoked_at is not None:
                revoked = await self.uow.refresh_tokens.revoke_all_for_user(replayed.user_id)
                await self.uow.commit()
                logger.warning(
                    "auth.refresh_token_replay",
                    user_id=str(replayed.user_id),
                    sessions_revoked=revoked,
                )
            raise InvalidRefreshTokenError()

        user = await self.uow.users.get_with_permissions(stored.user_id)
        if user is None or not user.is_active:
            raise InactiveUserError()

        stored.revoked_at = datetime.now(UTC)
        tokens = await self._issue_tokens(user, actor, replaces=stored)
        await self.uow.commit()
        return tokens

    # ---------------------------------------------------------------- logout
    async def logout(
        self, raw_token: str | None, user_id: UUID, *, all_sessions: bool, actor: ActorContext
    ) -> None:
        if all_sessions:
            await self.uow.refresh_tokens.revoke_all_for_user(user_id)
        elif raw_token:
            stored = await self.uow.refresh_tokens.get_active_by_hash(hash_refresh_token(raw_token))
            if stored is not None:
                stored.revoked_at = datetime.now(UTC)

        self.uow.audit.record(
            action=AuditAction.USER_LOGOUT,
            entity_type="User",
            entity_id=user_id,
            user_id=user_id,
            new_data={"all_sessions": all_sessions},
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            request_id=actor.request_id,
        )
        await self.uow.commit()

    # ------------------------------------------------------------------- me
    async def me(self, user_id: UUID) -> UserRead:
        user = await self.uow.users.get_with_permissions(user_id)
        if user is None:
            raise NotFoundError("User not found.", details={"user_id": str(user_id)})
        return await self._to_read(user)

    # ------------------------------------------------------------ internals
    async def _issue_tokens(
        self, user: User, actor: ActorContext, replaces: RefreshToken | None = None
    ) -> TokenPair:
        permissions = sorted(await self.uow.users.permission_codes(user.id))
        employee = await self.uow.employees.get_by_user_id(user.id)
        access_token, expires_at = create_access_token(
            user_id=user.id,
            permissions=permissions,
            is_superuser=user.is_superuser,
            employee_id=employee.id if employee else None,
        )
        raw_refresh, refresh_hash = generate_refresh_token()

        stored = RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=refresh_token_expiry(),
            user_agent=actor.user_agent,
            ip_address=actor.ip_address,
        )
        self.uow.refresh_tokens.add(stored)
        await self.uow.flush()

        if replaces is not None:
            replaces.replaced_by_id = stored.id

        return TokenPair(
            access_token=access_token,
            refresh_token=raw_refresh,
            expires_at=expires_at,
            expires_in=settings.access_token_expire_minutes * 60,
        )

    async def _to_read(self, user: User) -> UserRead:
        permissions = sorted(await self.uow.users.permission_codes(user.id))
        employee = await self.uow.employees.get_by_user_id(user.id)
        return UserRead(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
            is_superuser=user.is_superuser,
            last_login_at=user.last_login_at,
            created_at=user.created_at,
            roles=sorted(role.name for role in user.roles),
            permissions=permissions,
            employee_id=employee.id if employee else None,
            employee_number=employee.employee_number if employee else None,
            company_id=employee.company_id if employee else None,
        )


#: A real Argon2 hash of a random value, used to equalise login timing.
_DUMMY_HASH = hash_password("timing-equalisation-placeholder-value")

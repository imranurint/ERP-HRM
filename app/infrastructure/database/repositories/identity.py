"""User, role and refresh-token repositories."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.pagination import PageParams
from app.infrastructure.database.models.identity import (
    Permission,
    RefreshToken,
    Role,
    User,
)
from app.infrastructure.database.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        stmt = (
            select(User)
            .where(func.lower(User.email) == email.strip().lower())
            .options(selectinload(User.roles).selectinload(Role.permissions))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_with_permissions(self, user_id: UUID) -> User | None:
        stmt = (
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.roles).selectinload(Role.permissions))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def permission_codes(self, user_id: UUID) -> set[str]:
        """Flatten a user's role permissions into a set of codes — one query."""
        stmt = (
            select(Permission.code)
            .join(Permission.roles)
            .join(Role.users)
            .where(User.id == user_id)
            .distinct()
        )
        return set((await self.session.execute(stmt)).scalars().all())

    async def list_paginated(
        self, params: PageParams, *, is_active: bool | None = None
    ) -> tuple[Sequence[User], int]:
        stmt = select(User)
        if is_active is not None:
            stmt = stmt.where(User.is_active == is_active)
        total = await self.count(stmt)
        stmt = (
            stmt.options(selectinload(User.roles))
            .order_by(User.email)
            .offset(params.offset)
            .limit(params.limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return rows, total


class RoleRepository(BaseRepository[Role]):
    model = Role

    async def get_by_name(self, name: str) -> Role | None:
        stmt = select(Role).where(Role.name == name).options(selectinload(Role.permissions))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_with_permissions(self) -> Sequence[Role]:
        stmt = select(Role).options(selectinload(Role.permissions)).order_by(Role.name)
        return (await self.session.execute(stmt)).scalars().all()


class PermissionRepository(BaseRepository[Permission]):
    model = Permission

    async def get_by_code(self, code: str) -> Permission | None:
        stmt = select(Permission).where(Permission.code == code)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all_codes(self) -> Sequence[Permission]:
        stmt = select(Permission).order_by(Permission.code)
        return (await self.session.execute(stmt)).scalars().all()


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, token: RefreshToken) -> RefreshToken:
        self.session.add(token)
        return token

    async def get_active_by_hash(self, token_hash: str) -> RefreshToken | None:
        """Look up a *usable* token: not revoked and not expired."""
        stmt = select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(UTC),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def revoke_all_for_user(self, user_id: UUID) -> int:
        """Revoke every live token for a user — logout everywhere, or breach response."""
        stmt = select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
        tokens = (await self.session.execute(stmt)).scalars().all()
        now = datetime.now(UTC)
        for token in tokens:
            token.revoked_at = now
        return len(tokens)

    async def purge_expired(self, before: datetime | None = None) -> int:
        cutoff = before or datetime.now(UTC)
        stmt = select(RefreshToken).where(RefreshToken.expires_at < cutoff)
        rows = (await self.session.execute(stmt)).scalars().all()
        for row in rows:
            await self.session.delete(row)
        return len(rows)

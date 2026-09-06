"""Bootstrap the first administrator.

    python -m scripts.create_admin --email admin@example.com
    python -m scripts.create_admin --email a@b.com --password "$ADMIN_PASSWORD"

`POST /api/v1/auth/register` requires `users:manage`, so the very first user
has to be created out of band. If no password is supplied one is generated,
printed once, and never stored in plaintext or logged.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.modules.identity.domain.permissions import (
    DEFAULT_ROLE_PERMISSIONS,
    PERMISSION_DESCRIPTIONS,
    ROLE_DESCRIPTIONS,
    Permission,
    Role,
)
from app.modules.identity.infrastructure.models import (
    Permission as PermissionModel,
)
from app.modules.identity.infrastructure.models import (
    Role as RoleModel,
)
from app.modules.identity.infrastructure.models import (
    User,
)
from app.shared.database.session import dispose_engine, session_scope
from app.shared.unit_of_work.unit_of_work import UnitOfWork

logger = get_logger("create_admin")


async def ensure_roles_and_permissions(uow: UnitOfWork) -> dict[Role, RoleModel]:
    """Idempotently install the permission catalogue and default roles."""
    permission_models: dict[Permission, PermissionModel] = {}
    for permission in Permission:
        existing = await uow.permissions.get_by_code(str(permission))
        if existing is None:
            existing = PermissionModel(
                code=str(permission),
                description=PERMISSION_DESCRIPTIONS.get(permission),
            )
            uow.permissions.add(existing)
        permission_models[permission] = existing
    await uow.flush()

    roles: dict[Role, RoleModel] = {}
    for role, permissions in DEFAULT_ROLE_PERMISSIONS.items():
        model = await uow.roles.get_by_name(str(role))
        if model is None:
            # Attach the permissions before the flush: reading `.permissions`
            # off a freshly flushed instance would emit a lazy load, which is
            # blocking IO the async session cannot perform here.
            model = RoleModel(
                name=str(role),
                description=ROLE_DESCRIPTIONS.get(role),
                is_system=True,
            )
            model.permissions = [permission_models[p] for p in sorted(permissions)]
            uow.roles.add(model)
        else:
            # `get_by_name` eagerly loads permissions, so this is in memory.
            current = {perm.code for perm in model.permissions}
            for permission in sorted(permissions):
                if str(permission) not in current:
                    model.permissions.append(permission_models[permission])
        roles[role] = model

    await uow.flush()
    return roles


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the first admin user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", default=None, help="Omit to generate one.")
    parser.add_argument("--full-name", default="Administrator")
    parser.add_argument(
        "--superuser",
        action="store_true",
        default=True,
        help="Grant is_superuser (bypasses permission checks).",
    )
    args = parser.parse_args(argv)

    configure_logging()
    password = args.password or secrets.token_urlsafe(18)
    generated = args.password is None

    try:
        async with session_scope() as session:
            uow = UnitOfWork(session)
            roles = await ensure_roles_and_permissions(uow)

            existing = await uow.users.get_by_email(args.email)
            if existing is not None:
                print(f"User {args.email} already exists (id={existing.id}).")
                return 1

            user = User(
                email=args.email.strip().lower(),
                hashed_password=hash_password(password),
                full_name=args.full_name,
                is_active=True,
                is_superuser=args.superuser,
            )
            user.roles.append(roles[Role.ADMIN])
            uow.users.add(user)
            await uow.commit()

            print(f"Created admin {user.email} (id={user.id})")
            if generated:
                # Printed to stdout once. Never logged, never persisted.
                print(f"Generated password: {password}")
                print("Store it now — it cannot be recovered.")
    finally:
        await dispose_engine()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Test bootstrap.

Integration and API tests run against a **real PostgreSQL** — the schema uses
GiST exclusion constraints, partial unique indexes, `ON CONFLICT DO UPDATE`
and `SELECT ... FOR UPDATE`, none of which SQLite can emulate. Testing payroll
guarantees against a different database would test nothing.

By default a PostgreSQL 16 container is started via Testcontainers. Set
`USE_EXISTING_DATABASE=true` and `TEST_DATABASE_URL` (as CI does) to reuse an
already-running instance.

Isolation between tests is by TRUNCATE, not by an outer transaction: the
payroll and leave services commit, and wrapping them in a transaction that
never commits would hide exactly the concurrency behaviour we care about.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

# Settings are read at import time, so the environment must be complete first.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-0123456789abcdef")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("METRICS_ENABLED", "false")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://hrm:hrm@localhost:5432/hrm_test")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

USE_EXISTING = os.getenv("USE_EXISTING_DATABASE", "").lower() in {"1", "true", "yes"}


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A PostgreSQL URL for the whole session, container-backed if needed."""
    if USE_EXISTING:
        url = os.environ.get("TEST_DATABASE_URL") or os.environ["DATABASE_URL"]
        yield _as_asyncpg(url)
        return

    try:
        # Moved package in testcontainers 4.x; fall back for older installs.
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError:
            pytest.skip("testcontainers is not installed and USE_EXISTING_DATABASE is not set")

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


def _as_asyncpg(url: str) -> str:
    if "+asyncpg" in url:
        return url
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="session", autouse=True)
def configure_environment(database_url: str) -> Iterator[None]:
    """Point the application at the test database before anything connects."""
    os.environ["DATABASE_URL"] = database_url

    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.core.config as config_module

    config_module.settings = get_settings()

    yield


@pytest_asyncio.fixture(scope="session")
async def engine(configure_environment: None, database_url: str) -> AsyncIterator[AsyncEngine]:
    """Session-scoped engine with the schema applied via Alembic."""
    from app.infrastructure.database.session import set_session_factory

    test_engine = create_async_engine(database_url, pool_pre_ping=True, future=True)

    await _apply_migrations(database_url)

    factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    set_session_factory(factory)

    yield test_engine

    await test_engine.dispose()


async def _apply_migrations(database_url: str) -> None:
    """Run the real migrations, so the tests exercise the shipped schema."""
    import asyncio

    from alembic import command
    from alembic.config import Config

    sync_url = database_url.replace("+asyncpg", "+psycopg")

    def run() -> None:
        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "alembic")
        cfg.set_main_option("sqlalchemy.url", sync_url)
        command.upgrade(cfg, "head")

    await asyncio.to_thread(run)


#: Emptied between tests. Order is irrelevant thanks to CASCADE.
_TABLES = [
    "outbox_events",
    "audit_logs",
    "payslip_items",
    "payslips",
    "payroll_runs",
    "payroll_periods",
    "leave_balances",
    "leave_transactions",
    "leave_requests",
    "employee_leave_policies",
    "leave_policy_rules",
    "leave_policies",
    "leave_types",
    "attendance_records",
    "employee_shift_assignments",
    "shifts",
    "employee_salary_components",
    "employee_salary_structures",
    "salary_components",
    "employee_lifecycle_events",
    "employment_contracts",
    "employees",
    "holidays",
    "locations",
    "designations",
    "departments",
    "document_sequences",
    "companies",
    "refresh_tokens",
    "role_permissions",
    "user_roles",
    "permissions",
    "roles",
    "users",
    "currencies",
]


@pytest_asyncio.fixture(autouse=True)
async def clean_database(engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as db_session:
        yield db_session
        await db_session.close()


@pytest_asyncio.fixture
async def uow(session: AsyncSession) -> AsyncIterator[object]:
    from app.infrastructure.database.unit_of_work import UnitOfWork

    yield UnitOfWork(session)


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the ASGI app, sharing the test database."""
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def demo(session: AsyncSession) -> object:
    """A seeded company: currencies, org structure, employees, salary, leave."""
    from tests.factories import DemoData

    return await DemoData.create(session)

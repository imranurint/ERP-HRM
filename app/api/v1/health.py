"""Health, readiness and liveness probes.

* `/livez`  — is the process alive? No dependencies touched.
* `/readyz` — can it serve traffic? Verifies PostgreSQL, reports Redis.
* `/healthz` — human-facing summary.

Readiness fails on a database outage (nothing works without it) but not on a
Redis outage: rate limiting and caching degrade gracefully, and refusing
traffic would be worse than serving it uncached.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import Field
from sqlalchemy import text

from app.application.common import AppModel
from app.core.config import settings
from app.core.dates import utcnow
from app.core.deps import UowDep
from app.infrastructure.cache import redis as cache

router = APIRouter(tags=["health"])


class HealthStatus(AppModel):
    status: str
    version: str
    environment: str
    timestamp: datetime
    checks: dict[str, Any] = Field(default_factory=dict)


@router.get("/livez", summary="Liveness probe")
async def livez() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/readyz", summary="Readiness probe")
async def readyz(uow: UowDep, response: Response) -> HealthStatus:
    checks: dict[str, Any] = {}

    try:
        await uow.session.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok", "required": True}
        database_ok = True
    except Exception as exc:
        checks["database"] = {"status": "error", "required": True, "error": str(exc)}
        database_ok = False

    redis_ok = await cache.ping()
    checks["redis"] = {"status": "ok" if redis_ok else "degraded", "required": False}

    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthStatus(
        status="ready" if database_ok else "not_ready",
        version=settings.app_name,
        environment=settings.app_env,
        timestamp=utcnow(),
        checks=checks,
    )


@router.get("/healthz", summary="Health summary")
async def healthz(uow: UowDep, response: Response) -> HealthStatus:
    return await readyz(uow, response)

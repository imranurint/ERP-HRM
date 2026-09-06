"""Redis client.

Redis is strictly an optimisation here: rate limiting, cached lookups and an
idempotency short-circuit. Nothing accounting-critical lives in it, and every
caller must tolerate it being unavailable.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def ping() -> bool:
    try:
        client = await get_redis()
        return bool(await client.ping())
    except Exception as exc:
        logger.warning("redis.ping_failed", error=str(exc))
        return False


async def cache_get(key: str) -> str | None:
    """Read through, swallowing cache failures."""
    try:
        client = await get_redis()
        value: Any = await client.get(key)
        return str(value) if value is not None else None
    except Exception as exc:
        logger.warning("cache.get_failed", key=key, error=str(exc))
        return None


async def cache_set(key: str, value: str, ttl_seconds: int) -> None:
    try:
        client = await get_redis()
        await client.set(key, value, ex=ttl_seconds)
    except Exception as exc:
        logger.warning("cache.set_failed", key=key, error=str(exc))


async def cache_delete(*keys: str) -> None:
    if not keys:
        return
    try:
        client = await get_redis()
        await client.delete(*keys)
    except Exception as exc:
        logger.warning("cache.delete_failed", error=str(exc))

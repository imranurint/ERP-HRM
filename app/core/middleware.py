"""HTTP middleware: request identity, timing, body limits, rate limiting.

Ordering matters. Starlette runs middleware outermost-first, so we add them
in this order in `app.main`:

    RequestContextMiddleware   (assigns request_id, binds log context)
      → BodySizeLimitMiddleware
        → RateLimitMiddleware
          → application
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core.config import settings
from app.core.exceptions import PayloadTooLargeError, RateLimitedError
from app.core.ids import uuid7
from app.core.logging import bind_contextvars, clear_contextvars, get_logger

logger = get_logger(__name__)

REQUEST_ID_HEADER: Final = "X-Request-ID"
CORRELATION_ID_HEADER: Final = "X-Correlation-ID"

RequestHandler = Callable[[Request], Awaitable[Response]]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, propagate a correlation id, time the request.

    The correlation id is taken from the inbound header when present so a
    trace survives across ERP modules — an HR action that ends up posting a
    payroll journal in the Accounting module keeps one id end to end.
    Otherwise it defaults to the request id.
    """

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid7())
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or request_id

        request.state.request_id = request_id
        request.state.correlation_id = correlation_id

        clear_contextvars()
        bind_contextvars(
            request_id=request_id,
            correlation_id=correlation_id,
            method=request.method,
            path=request.url.path,
        )

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception("request.failed", duration_ms=round(duration_ms, 2))
            raise
        finally:
            clear_contextvars()

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CORRELATION_ID_HEADER] = correlation_id

        log = logger.bind(
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
            request_id=request_id,
            correlation_id=correlation_id,
            method=request.method,
            path=request.url.path,
        )
        if duration_ms >= settings.slow_request_ms:
            log.warning("request.slow")
        else:
            log.info("request.completed")
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before they reach the application."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = 0
            if declared > self.max_bytes:
                error = PayloadTooLargeError(
                    details={"max_bytes": self.max_bytes, "received_bytes": declared}
                )
                return JSONResponse(
                    status_code=error.status_code,
                    content=error.to_dict(getattr(request.state, "request_id", None)),
                )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limit per client, backed by Redis.

    Redis is an optimisation, not a correctness boundary: if it is unavailable
    the request is allowed through and the failure is logged. We never fail a
    payroll or attendance request because a cache is down.
    """

    def __init__(self, app: ASGIApp, *, requests: int, window_seconds: int) -> None:
        super().__init__(app)
        self.requests = requests
        self.window_seconds = window_seconds

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        if request.url.path in {"/healthz", "/livez", "/readyz", "/metrics"}:
            return await call_next(request)

        from app.shared.cache.keys import rate_limit_key
        from app.shared.cache.redis import get_redis

        identity = request.headers.get("authorization") or (
            request.client.host if request.client else "anonymous"
        )
        bucket = int(time.time()) // self.window_seconds
        key = rate_limit_key(identity, bucket)

        try:
            redis = await get_redis()
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, self.window_seconds)
        except Exception as exc:
            logger.warning("ratelimit.unavailable", error=str(exc))
            return await call_next(request)

        if count > self.requests:
            error = RateLimitedError(
                details={"limit": self.requests, "window_seconds": self.window_seconds}
            )
            return JSONResponse(
                status_code=error.status_code,
                content=error.to_dict(getattr(request.state, "request_id", None)),
                headers={"Retry-After": str(self.window_seconds)},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.requests)
        response.headers["X-RateLimit-Remaining"] = str(max(0, self.requests - count))
        return response

"""FastAPI application factory.

Wires middleware, exception handling, routing and observability. Business
logic lives nowhere near this file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import health
from app.api import api_router
from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
)
from app.shared.cache.redis import close_redis
from app.shared.database.session import dispose_engine
from app.shared.observability import metrics
from app.shared.observability.tracing import setup_sentry, setup_tracing

logger = get_logger(__name__)

DESCRIPTION = """
The **ERP HRM Core** — people, organisation structure, attendance, leave and
payroll for an ERP platform. Sibling to the Financial Core general ledger,
built to the same rules.

Key guarantees:

* payroll periods for a company never overlap, and neither do an employee's
  salary structures, shift assignments or leave-policy assignments — all four
  enforced by database exclusion constraints, not application checks;
* approved and paid payroll is immutable; corrections are new adjustment runs;
* every payslip stores the complete snapshot of inputs it was derived from;
* `leave_balances` is a projection, fully rebuildable from `leave_transactions`;
* employees are never hard-deleted — departure is a lifecycle status;
* money is `NUMERIC(19,4)` end to end, never a float, and crosses the API as
  a string.

Approving a payroll run emits a `PayrollApproved` event carrying pre-aggregated,
balanced journal lines, which the Accounting module consumes as a
`CreateJournalCommand`. HRM never writes to the general ledger itself.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    setup_sentry()
    setup_tracing(app)
    logger.info(
        "app.startup",
        environment=settings.app_env,
        openapi_enabled=settings.openapi_enabled,
        otel_enabled=settings.otel_enabled,
    )
    try:
        yield
    finally:
        await close_redis()
        await dispose_engine()
        logger.info("app.shutdown")


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.openapi_enabled else None,
        redoc_url="/redoc" if settings.openapi_enabled else None,
        openapi_url="/openapi.json" if settings.openapi_enabled else None,
    )

    _install_middleware(app)
    _install_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    if settings.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        async def prometheus_metrics() -> Response:
            payload, content_type = metrics.render_metrics()
            return Response(content=payload, media_type=content_type)

    return app


def _install_middleware(app: FastAPI) -> None:
    # Added last runs first: request context wraps everything so a request id
    # exists before any other middleware can fail.
    if settings.metrics_enabled:
        app.add_middleware(metrics.MetricsMiddleware)

    if settings.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            requests=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(RequestContextMiddleware)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID", "X-Correlation-ID"],
        )

    if settings.trusted_hosts and settings.trusted_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)


def _install_exception_handlers(app: FastAPI) -> None:
    """One error shape for every failure mode."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        log = logger.bind(error_code=exc.code, status_code=exc.status_code)
        if exc.status_code >= 500:
            log.exception("request.error")
        else:
            log.info("request.rejected", message=exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict(request_id))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "VALIDATION_ERROR",
                "message": "The request payload is invalid.",
                "details": {"errors": _serialisable_errors(exc.errors())},
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            409: "CONFLICT",
            429: "RATE_LIMITED",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": codes.get(exc.status_code, "HTTP_ERROR"),
                "message": str(exc.detail),
                "details": {},
                "request_id": getattr(request.state, "request_id", None),
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to the client; the details go to the logs.
        logger.exception("request.unhandled_exception", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={
                "error": "INTERNAL_ERROR",
                "message": "An unexpected error occurred.",
                "details": {},
                "request_id": getattr(request.state, "request_id", None),
            },
        )


def _serialisable_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Strip exception objects Pydantic embeds so the payload is JSON-safe."""
    cleaned: list[dict[str, Any]] = []
    for error in errors:
        item = {k: v for k, v in error.items() if k != "ctx"}
        item["loc"] = [str(part) for part in error.get("loc", ())]
        if "input" in item and not isinstance(item["input"], str | int | bool | type(None)):
            item["input"] = str(item["input"])
        cleaned.append(item)
    return cleaned


app = create_app()

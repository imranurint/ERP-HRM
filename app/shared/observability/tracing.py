"""OpenTelemetry wiring.

Entirely optional and off by default (`OTEL_ENABLED=false`). Instrumentation
failures are logged and swallowed: telemetry must never take down payroll.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_configured = False


def setup_tracing(app: Any) -> None:
    """Instrument FastAPI and SQLAlchemy, exporting over OTLP/gRPC."""
    global _configured
    if not settings.otel_enabled or _configured:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "deployment.environment": settings.app_env,
            }
        )
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,livez,readyz,metrics")

        from app.shared.database.session import get_engine

        SQLAlchemyInstrumentor().instrument(engine=get_engine().sync_engine)

        _configured = True
        logger.info(
            "otel.enabled",
            endpoint=settings.otel_exporter_otlp_endpoint,
            service=settings.otel_service_name,
        )
    except Exception as exc:
        logger.warning("otel.setup_failed", error=str(exc))


def setup_sentry() -> None:
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.app_env,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            # Bodies contain personal and salary data; never ship them off-box.
            send_default_pii=False,
            max_request_body_size="never",
        )
        logger.info("sentry.enabled", environment=settings.app_env)
    except Exception as exc:
        logger.warning("sentry.setup_failed", error=str(exc))

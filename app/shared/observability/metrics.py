"""Prometheus metrics.

HTTP metrics plus a small set of HR counters. Payroll and leave counters are
incremented where the work happens — in the services, after commit — so the
numbers reflect committed business activity rather than HTTP traffic.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REGISTRY = CollectorRegistry(auto_describe=True)

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests.",
    ["method", "path", "status"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

http_errors_total = Counter(
    "http_errors_total",
    "HTTP responses with a 4xx or 5xx status.",
    ["method", "path", "status"],
    registry=REGISTRY,
)

# --- payroll ---------------------------------------------------------------
payroll_runs_total = Counter(
    "payroll_runs_total",
    "Payroll runs by terminal status.",
    ["company_id", "status"],
    registry=REGISTRY,
)

payslips_generated_total = Counter(
    "payslips_generated_total",
    "Payslips successfully committed.",
    ["company_id"],
    registry=REGISTRY,
)

payroll_calculation_duration_seconds = Histogram(
    "payroll_calculation_duration_seconds",
    "End-to-end duration of the payroll calculation transaction.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)

payroll_failures_total = Counter(
    "payroll_failures_total",
    "Payroll operations rejected, labelled by business error code.",
    ["error_code"],
    registry=REGISTRY,
)

# --- people, attendance, leave ---------------------------------------------
employee_lifecycle_events_total = Counter(
    "employee_lifecycle_events_total",
    "Employee lifecycle transitions.",
    ["company_id", "event_type"],
    registry=REGISTRY,
)

attendance_records_total = Counter(
    "attendance_records_total",
    "Attendance records written, by source.",
    ["company_id", "source"],
    registry=REGISTRY,
)

leave_decisions_total = Counter(
    "leave_decisions_total",
    "Leave requests decided.",
    ["company_id", "decision"],
    registry=REGISTRY,
)

outbox_pending_events = Gauge(
    "outbox_pending_events",
    "Domain events waiting to be relayed.",
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def _route_template(request: Request) -> str:
    """Use the route pattern, not the raw path.

    `/api/v1/employees/{employee_id}` keeps cardinality bounded; the raw path
    would create one time series per employee.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path == "/metrics":
            return await call_next(request)

        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            duration = time.perf_counter() - started
            path = _route_template(request)
            http_request_duration_seconds.labels(request.method, path).observe(duration)
            http_requests_total.labels(request.method, path, str(status)).inc()
            if status >= 400:
                http_errors_total.labels(request.method, path, str(status)).inc()

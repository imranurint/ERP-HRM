"""Structured logging via structlog.

JSON in production, human-readable in local development. Request/correlation
identifiers are bound to a contextvar by the middleware so every log line
emitted while handling a request carries them automatically.

Never log secrets: passwords, raw refresh tokens, JWTs and API keys are
scrubbed by `_redact_processor` even when a caller passes them by accident.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars

from app.core.config import settings

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "hashed_password",
        "token",
        "access_token",
        "refresh_token",
        "token_hash",
        "secret",
        "secret_key",
        "authorization",
        "api_key",
        "private_key",
        # HR data is sensitive even when it is not a credential.
        "date_of_birth",
        "personal_email",
        "personal_phone",
        "national_id",
        "bank_account_number",
    }
)

REDACTED = "***redacted***"


def _redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def configure_logging() -> None:
    """Install the logging pipeline. Idempotent; safe to call more than once."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )
    for noisy in ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True

    processors: list[structlog.types.Processor] = [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_processor,
    ]
    if settings.log_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


__all__ = [
    "REDACTED",
    "bind_contextvars",
    "clear_contextvars",
    "configure_logging",
    "get_logger",
]

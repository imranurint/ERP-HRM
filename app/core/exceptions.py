"""Structured application errors.

Every error the API returns is a `Problem`-style JSON document:

    {
      "error": "PAYROLL_ALREADY_APPROVED",
      "message": "Approved payroll cannot be modified.",
      "details": {...},
      "request_id": "..."
    }

Business errors are raised deep in the domain/application layers and mapped to
HTTP by a single exception handler in `app.main`. Routes never build error
responses by hand.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every error this application raises deliberately."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.details: dict[str, Any] = details or {}
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)

    def to_dict(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
            "request_id": request_id,
        }


# --------------------------------------------------------------------------
# Generic HTTP-shaped errors
# --------------------------------------------------------------------------
class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    status_code = 422
    message = "The request payload is invalid."


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = 404
    message = "The requested resource does not exist."


class ConflictError(AppError):
    code = "CONFLICT"
    status_code = 409
    message = "The request conflicts with the current state of the resource."


class DuplicateResourceError(ConflictError):
    code = "DUPLICATE_RESOURCE"
    message = "A resource with that identifier already exists."


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    status_code = 401
    message = "Authentication is required."


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    status_code = 403
    message = "You do not have permission to perform this action."


class RateLimitedError(AppError):
    code = "RATE_LIMITED"
    status_code = 429
    message = "Too many requests."


class PayloadTooLargeError(AppError):
    code = "PAYLOAD_TOO_LARGE"
    status_code = 413
    message = "Request body exceeds the configured limit."


class ConcurrencyError(ConflictError):
    code = "CONCURRENT_MODIFICATION"
    message = "The resource was modified by another request. Reload and retry."


class ServiceUnavailableError(AppError):
    code = "SERVICE_UNAVAILABLE"
    status_code = 503
    message = "A required dependency is unavailable."


class CrossCompanyError(ForbiddenError):
    """Raised when a request tries to join data across two companies.

    Multi-company isolation is a correctness boundary, not a convenience: an
    employee from company A must never appear on company B's payroll, so this
    is checked explicitly wherever an id is accepted from a caller.
    """

    code = "CROSS_COMPANY_ACCESS"
    message = "The referenced resource belongs to a different company."

    @classmethod
    def for_entity(cls, entity_type: str, entity_id: object) -> CrossCompanyError:
        return cls(
            f"{entity_type} {entity_id} belongs to a different company.",
            details={"entity_type": entity_type, "entity_id": str(entity_id)},
        )

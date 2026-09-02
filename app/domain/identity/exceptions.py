"""Identity domain errors."""

from __future__ import annotations

from app.core.exceptions import AppError, ConflictError, ForbiddenError, UnauthorizedError


class InvalidCredentialsError(UnauthorizedError):
    code = "INVALID_CREDENTIALS"
    message = "Incorrect email or password."


class InactiveUserError(ForbiddenError):
    code = "USER_INACTIVE"
    message = "This account is disabled."


class EmailAlreadyRegisteredError(ConflictError):
    code = "EMAIL_ALREADY_REGISTERED"
    message = "An account with this email address already exists."


class InvalidRefreshTokenError(UnauthorizedError):
    code = "INVALID_REFRESH_TOKEN"
    message = "The refresh token is invalid, expired or revoked."


class PermissionDeniedError(ForbiddenError):
    code = "PERMISSION_DENIED"
    message = "You do not have permission to perform this action."

    @classmethod
    def for_permission(cls, permission: str) -> PermissionDeniedError:
        return cls(
            f"This action requires the '{permission}' permission.",
            details={"required_permission": permission},
        )


class NotYourRecordError(ForbiddenError):
    """Self-service scoping failure.

    Distinct from `PermissionDeniedError` on purpose: the caller *does* hold
    the permission, they are simply pointing it at somebody else's record.
    """

    code = "NOT_YOUR_RECORD"
    message = "You may only access your own record."


class RoleNotFoundError(AppError):
    code = "ROLE_NOT_FOUND"
    status_code = 404
    message = "The requested role does not exist."

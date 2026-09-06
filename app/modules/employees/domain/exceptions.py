"""Employee, contract and organisation domain errors."""

from __future__ import annotations

from app.core.exceptions import ConflictError, NotFoundError, ValidationError


class EmployeeNotFoundError(NotFoundError):
    code = "EMPLOYEE_NOT_FOUND"
    message = "The requested employee does not exist."


class EmployeeInactiveError(ConflictError):
    code = "EMPLOYEE_INACTIVE"
    message = "This employee is not currently employed."


class InvalidEmploymentStatusError(ConflictError):
    code = "INVALID_EMPLOYMENT_STATUS"
    message = "That employment status transition is not allowed."

    @classmethod
    def transition(cls, current: str, target: str) -> InvalidEmploymentStatusError:
        return cls(
            f"An employee cannot move from {current} to {target}.",
            details={"current_status": current, "target_status": target},
        )


class DuplicateEmployeeNumberError(ConflictError):
    code = "DUPLICATE_EMPLOYEE_NUMBER"
    message = "That employee number already exists for this company."


class DuplicateWorkEmailError(ConflictError):
    code = "DUPLICATE_WORK_EMAIL"
    message = "That work email address is already in use in this company."


class InvalidManagerError(ValidationError):
    code = "INVALID_MANAGER"
    message = "That manager assignment is not valid."


class InvalidHierarchyError(ValidationError):
    code = "INVALID_HIERARCHY"
    message = "That parent assignment would create a cycle."


class DepartmentNotFoundError(NotFoundError):
    code = "DEPARTMENT_NOT_FOUND"
    message = "The requested department does not exist."


class DesignationNotFoundError(NotFoundError):
    code = "DESIGNATION_NOT_FOUND"
    message = "The requested designation does not exist."


class LocationNotFoundError(NotFoundError):
    code = "LOCATION_NOT_FOUND"
    message = "The requested location does not exist."


class ContractNotFoundError(NotFoundError):
    code = "CONTRACT_NOT_FOUND"
    message = "The requested employment contract does not exist."


class InvalidContractError(ValidationError):
    code = "INVALID_CONTRACT"
    message = "The employment contract is not valid."


class ActiveContractExistsError(ConflictError):
    code = "ACTIVE_CONTRACT_EXISTS"
    message = "This employee already has an active employment contract."


class ContractImmutableError(ConflictError):
    code = "CONTRACT_IMMUTABLE"
    message = "An expired or terminated contract cannot be modified."

"""Employee and contract invariants.

Pure functions over plain values — no ORM, no session, no HTTP. That is what
makes the lifecycle rules testable without a database and reusable from a
future bulk-import job or an ERP module that never goes near the API.

Every function either returns normally or raises a domain error. None of them
return a boolean the caller might forget to check.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from uuid import UUID

from app.core.dates import days_inclusive
from app.domain.employees.enums import ContractStatus, ContractType, EmploymentStatus
from app.domain.employees.exceptions import (
    InvalidContractError,
    InvalidEmploymentStatusError,
    InvalidHierarchyError,
    InvalidManagerError,
)

#: The lifecycle graph. Everything not listed here is rejected.
#:
#: TERMINATED and RESIGNED are deliberately terminal — a returning employee is
#: a *rehire*, which gets a fresh employment record rather than resurrecting a
#: closed one. That keeps tenure, leave accrual and payroll history honest.
ALLOWED_STATUS_TRANSITIONS: Mapping[EmploymentStatus, frozenset[EmploymentStatus]] = {
    EmploymentStatus.PROBATION: frozenset(
        {
            EmploymentStatus.ACTIVE,
            EmploymentStatus.ON_LEAVE,
            EmploymentStatus.SUSPENDED,
            EmploymentStatus.TERMINATED,
            EmploymentStatus.RESIGNED,
            EmploymentStatus.INACTIVE,
        }
    ),
    EmploymentStatus.ACTIVE: frozenset(
        {
            EmploymentStatus.ON_LEAVE,
            EmploymentStatus.SUSPENDED,
            EmploymentStatus.TERMINATED,
            EmploymentStatus.RESIGNED,
            EmploymentStatus.INACTIVE,
        }
    ),
    EmploymentStatus.ON_LEAVE: frozenset(
        {
            EmploymentStatus.ACTIVE,
            EmploymentStatus.SUSPENDED,
            EmploymentStatus.TERMINATED,
            EmploymentStatus.RESIGNED,
            EmploymentStatus.INACTIVE,
        }
    ),
    EmploymentStatus.SUSPENDED: frozenset(
        {
            EmploymentStatus.ACTIVE,
            EmploymentStatus.TERMINATED,
            EmploymentStatus.RESIGNED,
            EmploymentStatus.INACTIVE,
        }
    ),
    EmploymentStatus.INACTIVE: frozenset(
        {
            EmploymentStatus.ACTIVE,
            EmploymentStatus.PROBATION,
            EmploymentStatus.TERMINATED,
            EmploymentStatus.RESIGNED,
        }
    ),
    EmploymentStatus.TERMINATED: frozenset(),
    EmploymentStatus.RESIGNED: frozenset(),
}

#: How far ahead a hire date may be dated. Onboarding a starter a quarter
#: ahead is normal; a hire date in 2087 is a typo.
MAX_FUTURE_HIRE_DAYS = 365

MAX_PROBATION_DAYS = 730


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
def ensure_status_transition(current: EmploymentStatus, target: EmploymentStatus) -> None:
    """Reject a lifecycle move the graph does not permit."""
    if current is target:
        raise InvalidEmploymentStatusError(
            f"The employee is already {current}.",
            details={"current_status": str(current), "target_status": str(target)},
        )
    if target not in ALLOWED_STATUS_TRANSITIONS.get(current, frozenset()):
        raise InvalidEmploymentStatusError.transition(str(current), str(target))


def ensure_employed(status: EmploymentStatus, *, action: str) -> None:
    """Guard an action that only makes sense for somebody still employed."""
    if not status.is_employed:
        raise InvalidEmploymentStatusError(
            f"Cannot {action}: the employee's status is {status}.",
            details={"current_status": str(status), "action": action},
        )


def ensure_hire_date(hire_date: date, *, today: date) -> None:
    if (hire_date - today).days > MAX_FUTURE_HIRE_DAYS:
        raise InvalidContractError(
            "Hire date is too far in the future.",
            details={
                "hire_date": hire_date.isoformat(),
                "max_days_ahead": MAX_FUTURE_HIRE_DAYS,
            },
        )


def ensure_termination_date(hire_date: date, termination_date: date) -> None:
    if termination_date < hire_date:
        raise InvalidContractError(
            "Termination date cannot precede the hire date.",
            details={
                "hire_date": hire_date.isoformat(),
                "termination_date": termination_date.isoformat(),
            },
        )


def ensure_date_of_birth(date_of_birth: date, hire_date: date) -> None:
    """A person cannot be hired before they were born, or on their birthday.

    Deliberately not a minimum-age check: the legal working age varies by
    jurisdiction and belongs in deployment configuration, not in a rule that
    would silently reject a lawful hire somewhere else.
    """
    if date_of_birth >= hire_date:
        raise InvalidContractError(
            "Date of birth must precede the hire date.",
            details={
                "date_of_birth": date_of_birth.isoformat(),
                "hire_date": hire_date.isoformat(),
            },
        )


# --------------------------------------------------------------------------
# Hierarchies
# --------------------------------------------------------------------------
def ensure_not_self_managed(employee_id: UUID, manager_id: UUID | None) -> None:
    if manager_id is not None and manager_id == employee_id:
        raise InvalidManagerError(
            "An employee cannot be their own manager.",
            details={"employee_id": str(employee_id)},
        )


def ensure_no_manager_cycle(
    employee_id: UUID,
    manager_id: UUID | None,
    manager_chain: Iterable[UUID],
) -> None:
    """Reject a reporting line that loops back to the employee.

    `manager_chain` is the proposed manager's own chain of managers, walked by
    the repository. If the employee appears in it, the assignment would create
    a cycle and every org-chart traversal after it would never terminate.
    """
    ensure_not_self_managed(employee_id, manager_id)
    if manager_id is None:
        return
    for ancestor in manager_chain:
        if ancestor == employee_id:
            raise InvalidHierarchyError(
                "That manager reports to this employee, directly or indirectly.",
                details={"employee_id": str(employee_id), "manager_id": str(manager_id)},
            )


def ensure_no_department_cycle(
    department_id: UUID,
    parent_id: UUID | None,
    ancestor_chain: Iterable[UUID],
) -> None:
    """Same guarantee for the department tree."""
    if parent_id is None:
        return
    if parent_id == department_id:
        raise InvalidHierarchyError(
            "A department cannot be its own parent.",
            details={"department_id": str(department_id)},
        )
    for ancestor in ancestor_chain:
        if ancestor == department_id:
            raise InvalidHierarchyError(
                "That parent department is a descendant of this one.",
                details={
                    "department_id": str(department_id),
                    "parent_department_id": str(parent_id),
                },
            )


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------
def ensure_contract_dates(
    *,
    contract_type: ContractType,
    start_date: date,
    end_date: date | None,
    probation_end_date: date | None,
) -> None:
    """Validate the shape of a contract's dates.

    Fixed-term types must carry an end date. Without one, "expired" is
    unknowable and the contract would sit ACTIVE forever.
    """
    if end_date is not None and end_date < start_date:
        raise InvalidContractError(
            "Contract end date cannot precede its start date.",
            details={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )

    if contract_type.requires_end_date and end_date is None:
        raise InvalidContractError(
            f"A {contract_type} contract must have an end date.",
            details={"contract_type": str(contract_type)},
        )

    if probation_end_date is None:
        return

    if probation_end_date < start_date:
        raise InvalidContractError(
            "Probation cannot end before the contract starts.",
            details={
                "start_date": start_date.isoformat(),
                "probation_end_date": probation_end_date.isoformat(),
            },
        )
    if end_date is not None and probation_end_date > end_date:
        raise InvalidContractError(
            "Probation cannot end after the contract does.",
            details={
                "probation_end_date": probation_end_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )
    if days_inclusive(start_date, probation_end_date) > MAX_PROBATION_DAYS:
        raise InvalidContractError(
            "Probation period is implausibly long.",
            details={"max_days": MAX_PROBATION_DAYS},
        )


def ensure_contract_mutable(status: ContractStatus) -> None:
    from app.domain.employees.exceptions import ContractImmutableError

    if status.is_final:
        raise ContractImmutableError(
            f"A {status} contract cannot be modified.",
            details={"status": str(status)},
        )


def ensure_working_hours(hours_per_week: int | None) -> None:
    if hours_per_week is None:
        return
    if not 1 <= hours_per_week <= 168:
        raise InvalidContractError(
            "Working hours per week must be between 1 and 168.",
            details={"working_hours_per_week": hours_per_week},
        )


def contract_status_on(
    *, status: ContractStatus, end_date: date | None, as_of: date
) -> ContractStatus:
    """The status a contract *should* carry on a given date.

    Expiry is derived rather than driven by a scheduled job: a contract whose
    end date has passed reads as EXPIRED even if no batch has run, so payroll
    can never pay against a lapsed contract because a cron job was down.
    """
    if status is ContractStatus.ACTIVE and end_date is not None and end_date < as_of:
        return ContractStatus.EXPIRED
    return status

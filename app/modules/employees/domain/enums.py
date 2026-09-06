"""Employee, contract and organisation enumerations."""

from __future__ import annotations

from enum import StrEnum


class EmploymentStatus(StrEnum):
    """Where an employee sits in their lifecycle.

    Employees are never hard-deleted. Departure is a status, so the person's
    attendance, leave and payroll history stays intact and reportable.
    """

    ACTIVE = "ACTIVE"
    PROBATION = "PROBATION"
    ON_LEAVE = "ON_LEAVE"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    RESIGNED = "RESIGNED"
    INACTIVE = "INACTIVE"

    @property
    def is_employed(self) -> bool:
        """Still on the books — counts for headcount and payroll eligibility."""
        return self in (
            EmploymentStatus.ACTIVE,
            EmploymentStatus.PROBATION,
            EmploymentStatus.ON_LEAVE,
        )

    @property
    def is_terminal(self) -> bool:
        """Employment has ended. Terminal states are not re-enterable."""
        return self in (EmploymentStatus.TERMINATED, EmploymentStatus.RESIGNED)

    @property
    def accrues_leave(self) -> bool:
        """Suspended employees keep their balance but stop earning entitlement."""
        return self in (EmploymentStatus.ACTIVE, EmploymentStatus.PROBATION)

    @property
    def is_payable(self) -> bool:
        """Eligible to appear on a payroll run at all.

        A suspended employee is deliberately included: whether suspension is
        paid is a policy question answered by their salary structure, not by
        the payroll engine.
        """
        return self is not EmploymentStatus.INACTIVE


class Gender(StrEnum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"
    UNDISCLOSED = "UNDISCLOSED"


class ContractType(StrEnum):
    PERMANENT = "PERMANENT"
    CONTRACT = "CONTRACT"
    TEMPORARY = "TEMPORARY"
    INTERN = "INTERN"
    CONSULTANT = "CONSULTANT"

    @property
    def requires_end_date(self) -> bool:
        """Only a permanent contract may be open-ended."""
        return self is not ContractType.PERMANENT


class ContractStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    TERMINATED = "TERMINATED"

    @property
    def is_current(self) -> bool:
        return self is ContractStatus.ACTIVE

    @property
    def is_final(self) -> bool:
        return self in (ContractStatus.EXPIRED, ContractStatus.TERMINATED)


class LifecycleEventType(StrEnum):
    """Every entry in `employee_lifecycle_events`.

    This table is append-only and is the answer to "why does this person's
    record look like this?" — a question payroll disputes and audits ask
    constantly.
    """

    HIRED = "HIRED"
    ONBOARDED = "ONBOARDED"
    PROBATION_COMPLETED = "PROBATION_COMPLETED"
    PROMOTED = "PROMOTED"
    TRANSFERRED = "TRANSFERRED"
    SALARY_CHANGED = "SALARY_CHANGED"
    CONTRACT_CHANGED = "CONTRACT_CHANGED"
    SUSPENDED = "SUSPENDED"
    REINSTATED = "REINSTATED"
    ON_LEAVE = "ON_LEAVE"
    RETURNED_FROM_LEAVE = "RETURNED_FROM_LEAVE"
    RESIGNED = "RESIGNED"
    TERMINATED = "TERMINATED"

"""Payroll enumerations."""

from __future__ import annotations

from enum import StrEnum


class SalaryComponentType(StrEnum):
    """What a component does to the payslip.

    EMPLOYER_CONTRIBUTION is neither an earning nor a deduction from the
    employee's perspective: it costs the employer money but never touches
    gross or net. Keeping it a distinct type is what stops it being quietly
    added into either.
    """

    EARNING = "EARNING"
    DEDUCTION = "DEDUCTION"
    EMPLOYER_CONTRIBUTION = "EMPLOYER_CONTRIBUTION"

    @property
    def affects_gross(self) -> bool:
        return self is SalaryComponentType.EARNING

    @property
    def affects_net(self) -> bool:
        return self in (SalaryComponentType.EARNING, SalaryComponentType.DEDUCTION)

    @property
    def sign(self) -> int:
        """+1 adds to net, -1 subtracts, 0 sits outside the employee's pay."""
        if self is SalaryComponentType.EARNING:
            return 1
        if self is SalaryComponentType.DEDUCTION:
            return -1
        return 0


class CalculationType(StrEnum):
    """How a component's amount is arrived at."""

    FIXED = "FIXED"
    PERCENTAGE = "PERCENTAGE"
    FORMULA = "FORMULA"


class PercentageBase(StrEnum):
    """What a PERCENTAGE component is a percentage *of*.

    Made explicit rather than assumed: "10% pension" means something quite
    different against base salary than against gross earnings, and getting it
    wrong is a silent, recurring payroll error.
    """

    BASE_SALARY = "BASE_SALARY"
    GROSS_EARNINGS = "GROSS_EARNINGS"
    TAXABLE_EARNINGS = "TAXABLE_EARNINGS"


class SalaryStructureStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"

    @property
    def is_payable(self) -> bool:
        return self is SalaryStructureStatus.ACTIVE


class PayrollPeriodStatus(StrEnum):
    OPEN = "OPEN"
    PROCESSING = "PROCESSING"
    APPROVED = "APPROVED"
    PAID = "PAID"
    CLOSED = "CLOSED"

    @property
    def accepts_runs(self) -> bool:
        """Only an OPEN period may start new payroll work."""
        return self is PayrollPeriodStatus.OPEN

    @property
    def is_locked(self) -> bool:
        """Attendance and leave feeding this period can no longer change."""
        return self in (
            PayrollPeriodStatus.APPROVED,
            PayrollPeriodStatus.PAID,
            PayrollPeriodStatus.CLOSED,
        )


class PayrollRunStatus(StrEnum):
    DRAFT = "DRAFT"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    APPROVED = "APPROVED"
    PAID = "PAID"
    FAILED = "FAILED"

    @property
    def is_mutable(self) -> bool:
        """Can payslips still be recalculated?

        FAILED is mutable on purpose: a run that blew up halfway is meant to
        be fixed and re-run, not abandoned.
        """
        return self in (
            PayrollRunStatus.DRAFT,
            PayrollRunStatus.COMPLETED,
            PayrollRunStatus.FAILED,
        )

    @property
    def is_approved(self) -> bool:
        return self in (PayrollRunStatus.APPROVED, PayrollRunStatus.PAID)

    @property
    def is_final(self) -> bool:
        return self is PayrollRunStatus.PAID


class PayslipStatus(StrEnum):
    DRAFT = "DRAFT"
    CALCULATED = "CALCULATED"
    APPROVED = "APPROVED"
    PAID = "PAID"
    VOID = "VOID"

    @property
    def is_mutable(self) -> bool:
        return self in (PayslipStatus.DRAFT, PayslipStatus.CALCULATED)

    @property
    def counts_toward_totals(self) -> bool:
        """A voided payslip is kept for audit but excluded from every total."""
        return self is not PayslipStatus.VOID

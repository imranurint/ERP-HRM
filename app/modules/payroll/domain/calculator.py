"""The payslip calculator.

A pure function: resolved inputs in, payslip lines out. No database, no clock,
no configuration lookups. Given the same `PayrollInput` it produces byte-identical
output on any machine, forever — which is Payroll Golden Rule 4, and the only
reason recalculating a two-year-old period is a safe thing to do.

**Evaluation order.** Components are evaluated in `sequence` order (ties broken
by `component_code`, so the order is total and stable), and each component sees
the running totals of everything evaluated *before* it. That makes
"pension is 5% of gross" unambiguous: it means 5% of the gross accumulated so
far, and HR controls what "so far" includes by setting the sequence. The
alternative — resolving a dependency graph — would make the same configuration
mean different things depending on which components happened to exist.

Rounding happens once per line, at the currency's own scale. Totals are sums of
already-rounded lines, so a payslip's parts always add up to its totals exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.money import ZERO, percentage_of, quantize_currency
from app.modules.payroll.domain.enums import CalculationType, PercentageBase, SalaryComponentType
from app.modules.payroll.domain.exceptions import (
    NegativeNetPayError,
    SalaryComponentInvalidError,
)
from app.modules.payroll.domain.formulas import FormulaContext, resolve

#: `salary_component_id` is NULL on this line — base salary is a property of
#: the structure, not a configurable component.
BASIC_SALARY_CODE = "BASIC"
BASIC_SALARY_NAME = "Basic Salary"


@dataclass(frozen=True, slots=True)
class ResolvedComponent:
    """A salary component with the employee's own amount already attached."""

    component_id: UUID
    component_code: str
    name: str
    component_type: SalaryComponentType
    calculation_type: CalculationType
    sequence: int
    is_taxable: bool
    amount: Decimal = ZERO
    percentage: Decimal | None = None
    percentage_base: PercentageBase = PercentageBase.BASE_SALARY
    formula_key: str | None = None
    gl_account_code: str | None = None

    def sort_key(self) -> tuple[int, str]:
        return (self.sequence, self.component_code)


@dataclass(frozen=True, slots=True)
class AttendanceSummary:
    """The attendance facts payroll consumes.

    `working_days` is the divisor for proration; `payable_days` is the
    numerator. Both come from the attendance module, already netted for
    weekends, holidays and paid leave.
    """

    working_days: Decimal
    payable_days: Decimal
    unpaid_leave_days: Decimal = ZERO
    overtime_minutes: int = 0

    @property
    def proration_factor(self) -> Decimal:
        """`payable_days / working_days`, clamped to [0, 1].

        Clamped on purpose: attendance data that claims more payable days than
        the month contains is a bug upstream, and paying 110% of salary is a
        worse failure than paying 100%.
        """
        if self.working_days <= 0:
            return Decimal("1")
        factor = self.payable_days / self.working_days
        if factor < 0:
            return ZERO
        return min(factor, Decimal("1"))


@dataclass(frozen=True, slots=True)
class PayrollInput:
    """Everything needed to compute exactly one payslip."""

    employee_id: UUID
    base_salary: Decimal
    currency_code: str
    currency_decimal_places: int
    components: tuple[ResolvedComponent, ...]
    attendance: AttendanceSummary
    prorate_base_salary: bool = True


@dataclass(frozen=True, slots=True)
class PayslipLine:
    """One `payslip_items` row, with the reasoning that produced it."""

    component_id: UUID | None
    component_code: str
    component_name: str
    component_type: SalaryComponentType
    amount: Decimal
    is_taxable: bool
    sequence: int
    gl_account_code: str | None
    calculation_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PayslipResult:
    """The computed payslip: lines plus the totals derived from them."""

    lines: tuple[PayslipLine, ...]
    gross_salary: Decimal
    total_deductions: Decimal
    employer_contributions: Decimal
    net_salary: Decimal
    snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class _RunningTotals:
    """Mutable accumulator threaded through the evaluation."""

    gross: Decimal = ZERO
    taxable: Decimal = ZERO
    deductions: Decimal = ZERO
    employer: Decimal = ZERO

    def add(self, line: PayslipLine) -> None:
        if line.component_type is SalaryComponentType.EARNING:
            self.gross += line.amount
            if line.is_taxable:
                self.taxable += line.amount
        elif line.component_type is SalaryComponentType.DEDUCTION:
            self.deductions += line.amount
        else:
            self.employer += line.amount


def calculate(payroll_input: PayrollInput) -> PayslipResult:
    """Compute a payslip. Deterministic and side-effect free."""
    places = payroll_input.currency_decimal_places
    attendance = payroll_input.attendance

    factor = attendance.proration_factor if payroll_input.prorate_base_salary else Decimal("1")
    basic = quantize_currency(payroll_input.base_salary * factor, places)

    lines: list[PayslipLine] = [
        PayslipLine(
            component_id=None,
            component_code=BASIC_SALARY_CODE,
            component_name=BASIC_SALARY_NAME,
            component_type=SalaryComponentType.EARNING,
            amount=basic,
            is_taxable=True,
            sequence=0,
            gl_account_code=None,
            calculation_metadata={
                "calculation_type": str(CalculationType.FIXED),
                "contractual_base_salary": str(payroll_input.base_salary),
                "prorated": payroll_input.prorate_base_salary,
                "proration_factor": str(factor),
                "working_days": str(attendance.working_days),
                "payable_days": str(attendance.payable_days),
            },
        )
    ]

    totals = _RunningTotals()
    totals.add(lines[0])

    for component in sorted(payroll_input.components, key=lambda c: c.sort_key()):
        line = _evaluate(component, payroll_input, totals, places)
        lines.append(line)
        totals.add(line)

    net = totals.gross - totals.deductions
    if net < 0:
        raise NegativeNetPayError(
            "Deductions exceed earnings for this employee; net pay would be negative.",
            details={
                "employee_id": str(payroll_input.employee_id),
                "gross_salary": str(totals.gross),
                "total_deductions": str(totals.deductions),
                "net_salary": str(net),
            },
        )

    return PayslipResult(
        lines=tuple(lines),
        gross_salary=quantize_currency(totals.gross, places),
        total_deductions=quantize_currency(totals.deductions, places),
        employer_contributions=quantize_currency(totals.employer, places),
        net_salary=quantize_currency(net, places),
        snapshot={
            "currency_code": payroll_input.currency_code,
            "currency_decimal_places": places,
            "contractual_base_salary": str(payroll_input.base_salary),
            "prorate_base_salary": payroll_input.prorate_base_salary,
            "proration_factor": str(factor),
            "working_days": str(attendance.working_days),
            "payable_days": str(attendance.payable_days),
            "unpaid_leave_days": str(attendance.unpaid_leave_days),
            "overtime_minutes": attendance.overtime_minutes,
            "component_order": [
                component.component_code
                for component in sorted(payroll_input.components, key=lambda c: c.sort_key())
            ],
            "taxable_earnings": str(quantize_currency(totals.taxable, places)),
        },
    )


def _evaluate(
    component: ResolvedComponent,
    payroll_input: PayrollInput,
    totals: _RunningTotals,
    places: int,
) -> PayslipLine:
    metadata: dict[str, Any] = {
        "calculation_type": str(component.calculation_type),
        "component_type": str(component.component_type),
    }

    match component.calculation_type:
        case CalculationType.FIXED:
            amount = quantize_currency(component.amount, places)
            metadata["configured_amount"] = str(component.amount)

        case CalculationType.PERCENTAGE:
            if component.percentage is None:
                raise SalaryComponentInvalidError(
                    f"Component {component.component_code} is PERCENTAGE but has no percentage.",
                    details={"component_code": component.component_code},
                )
            base = _percentage_base_value(component.percentage_base, payroll_input, totals, places)
            amount = percentage_of(base, component.percentage, places)
            metadata["percentage"] = str(component.percentage)
            metadata["percentage_base"] = str(component.percentage_base)
            metadata["base_amount"] = str(base)

        case CalculationType.FORMULA:
            if not component.formula_key:
                raise SalaryComponentInvalidError(
                    f"Component {component.component_code} is FORMULA but names no formula.",
                    details={"component_code": component.component_code},
                )
            context = FormulaContext(
                base_salary=payroll_input.base_salary,
                gross_earnings=totals.gross,
                taxable_earnings=totals.taxable,
                total_deductions=totals.deductions,
                working_days=payroll_input.attendance.working_days,
                payable_days=payroll_input.attendance.payable_days,
                overtime_minutes=payroll_input.attendance.overtime_minutes,
                currency_decimal_places=places,
            )
            amount = quantize_currency(resolve(component.formula_key)(context), places)
            metadata["formula_key"] = component.formula_key
            metadata["formula_inputs"] = {
                "base_salary": str(context.base_salary),
                "gross_earnings": str(context.gross_earnings),
                "taxable_earnings": str(context.taxable_earnings),
                "working_days": str(context.working_days),
                "payable_days": str(context.payable_days),
                "overtime_minutes": context.overtime_minutes,
            }

    if amount < 0:
        raise SalaryComponentInvalidError(
            f"Component {component.component_code} produced a negative amount; "
            "use a DEDUCTION component instead of a negative earning.",
            details={"component_code": component.component_code, "amount": str(amount)},
        )

    return PayslipLine(
        component_id=component.component_id,
        component_code=component.component_code,
        component_name=component.name,
        component_type=component.component_type,
        amount=amount,
        is_taxable=component.is_taxable,
        sequence=component.sequence,
        gl_account_code=component.gl_account_code,
        calculation_metadata=metadata,
    )


def _percentage_base_value(
    base: PercentageBase,
    payroll_input: PayrollInput,
    totals: _RunningTotals,
    places: int,
) -> Decimal:
    match base:
        case PercentageBase.BASE_SALARY:
            # The *contractual* salary, not the prorated basic: a 5% allowance
            # should not shrink because somebody took a day off.
            return quantize_currency(payroll_input.base_salary, places)
        case PercentageBase.GROSS_EARNINGS:
            return quantize_currency(totals.gross, places)
        case PercentageBase.TAXABLE_EARNINGS:
            return quantize_currency(totals.taxable, places)

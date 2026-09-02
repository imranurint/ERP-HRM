"""Payroll calculation — the arithmetic everything else depends on.

No database. These tests pin the properties that make payroll trustworthy:
determinism, exact Decimal arithmetic, a defined evaluation order, and totals
that always reconcile with their parts.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.money import allocate, percentage_of, quantize_currency
from app.domain.payroll.calculator import (
    AttendanceSummary,
    PayrollInput,
    ResolvedComponent,
    calculate,
)
from app.domain.payroll.enums import CalculationType, PercentageBase, SalaryComponentType
from app.domain.payroll.exceptions import NegativeNetPayError, SalaryComponentInvalidError

pytestmark = pytest.mark.unit

FULL_MONTH = AttendanceSummary(working_days=Decimal("22"), payable_days=Decimal("22"))


def component(
    code: str,
    kind: SalaryComponentType,
    calc: CalculationType,
    *,
    sequence: int = 100,
    amount: Decimal = Decimal("0"),
    percentage: Decimal | None = None,
    base: PercentageBase = PercentageBase.BASE_SALARY,
    formula_key: str | None = None,
    taxable: bool = True,
) -> ResolvedComponent:
    return ResolvedComponent(
        component_id=uuid4(),
        component_code=code,
        name=code.title(),
        component_type=kind,
        calculation_type=calc,
        sequence=sequence,
        is_taxable=taxable,
        amount=amount,
        percentage=percentage,
        percentage_base=base,
        formula_key=formula_key,
    )


def payroll_input(
    *components: ResolvedComponent,
    base_salary: Decimal = Decimal("4000.00"),
    attendance: AttendanceSummary = FULL_MONTH,
    prorate: bool = True,
    decimal_places: int = 2,
) -> PayrollInput:
    return PayrollInput(
        employee_id=uuid4(),
        base_salary=base_salary,
        currency_code="GBP",
        currency_decimal_places=decimal_places,
        components=components,
        attendance=attendance,
        prorate_base_salary=prorate,
    )


class TestTotals:
    def test_basic_salary_only(self) -> None:
        result = calculate(payroll_input())
        assert result.gross_salary == Decimal("4000.0000")
        assert result.total_deductions == Decimal("0.0000")
        assert result.net_salary == Decimal("4000.0000")
        assert len(result.lines) == 1
        assert result.lines[0].component_code == "BASIC"
        # Base salary is not a configurable component, so it carries no id.
        assert result.lines[0].component_id is None

    def test_net_equals_gross_minus_deductions(self) -> None:
        result = calculate(
            payroll_input(
                component(
                    "HOUSING",
                    SalaryComponentType.EARNING,
                    CalculationType.PERCENTAGE,
                    sequence=10,
                    percentage=Decimal("15"),
                ),
                component(
                    "PENSION",
                    SalaryComponentType.DEDUCTION,
                    CalculationType.PERCENTAGE,
                    sequence=40,
                    percentage=Decimal("5"),
                    base=PercentageBase.GROSS_EARNINGS,
                ),
            )
        )
        assert result.net_salary == result.gross_salary - result.total_deductions

    def test_line_amounts_sum_to_totals(self) -> None:
        """The invariant a payslip is judged on: parts add up to the whole."""
        result = calculate(
            payroll_input(
                component(
                    "HOUSING",
                    SalaryComponentType.EARNING,
                    CalculationType.PERCENTAGE,
                    sequence=10,
                    percentage=Decimal("15"),
                ),
                component(
                    "TRANSPORT",
                    SalaryComponentType.EARNING,
                    CalculationType.FIXED,
                    sequence=20,
                    amount=Decimal("150.00"),
                ),
                component(
                    "PENSION",
                    SalaryComponentType.DEDUCTION,
                    CalculationType.PERCENTAGE,
                    sequence=40,
                    percentage=Decimal("5"),
                ),
            )
        )
        earnings = sum(
            line.amount
            for line in result.lines
            if line.component_type is SalaryComponentType.EARNING
        )
        deductions = sum(
            line.amount
            for line in result.lines
            if line.component_type is SalaryComponentType.DEDUCTION
        )
        assert earnings == result.gross_salary
        assert deductions == result.total_deductions

    def test_employer_contributions_touch_neither_gross_nor_net(self) -> None:
        """An employer cost is real money, but not the employee's money."""
        without = calculate(payroll_input())
        with_er = calculate(
            payroll_input(
                component(
                    "NI_ER",
                    SalaryComponentType.EMPLOYER_CONTRIBUTION,
                    CalculationType.PERCENTAGE,
                    percentage=Decimal("13.8"),
                )
            )
        )
        assert with_er.gross_salary == without.gross_salary
        assert with_er.net_salary == without.net_salary
        assert with_er.employer_contributions == Decimal("552.0000")


class TestEvaluationOrder:
    def test_percentage_of_gross_sees_earlier_components_only(self) -> None:
        """Sequence defines what "gross so far" means — that is the contract."""
        result = calculate(
            payroll_input(
                component(
                    "HOUSING",
                    SalaryComponentType.EARNING,
                    CalculationType.PERCENTAGE,
                    sequence=10,
                    percentage=Decimal("15"),
                ),
                component(
                    "PENSION",
                    SalaryComponentType.DEDUCTION,
                    CalculationType.PERCENTAGE,
                    sequence=40,
                    percentage=Decimal("10"),
                    base=PercentageBase.GROSS_EARNINGS,
                ),
            )
        )
        # basic 4000 + housing 600 = 4600 gross; pension is 10% of that.
        pension = next(line for line in result.lines if line.component_code == "PENSION")
        assert pension.amount == Decimal("460.0000")

    def test_a_later_earning_does_not_retroactively_change_an_earlier_deduction(self) -> None:
        result = calculate(
            payroll_input(
                component(
                    "PENSION",
                    SalaryComponentType.DEDUCTION,
                    CalculationType.PERCENTAGE,
                    sequence=10,
                    percentage=Decimal("10"),
                    base=PercentageBase.GROSS_EARNINGS,
                ),
                component(
                    "BONUS",
                    SalaryComponentType.EARNING,
                    CalculationType.FIXED,
                    sequence=90,
                    amount=Decimal("1000.00"),
                ),
            )
        )
        pension = next(line for line in result.lines if line.component_code == "PENSION")
        # 10% of basic alone — the bonus comes after it.
        assert pension.amount == Decimal("400.0000")

    def test_order_is_total_and_stable(self) -> None:
        """Equal sequences tie-break on code, so the order never wobbles."""
        first = calculate(
            payroll_input(
                component(
                    "BBB",
                    SalaryComponentType.EARNING,
                    CalculationType.FIXED,
                    sequence=10,
                    amount=Decimal("10"),
                ),
                component(
                    "AAA",
                    SalaryComponentType.EARNING,
                    CalculationType.FIXED,
                    sequence=10,
                    amount=Decimal("20"),
                ),
            )
        )
        assert [line.component_code for line in first.lines] == ["BASIC", "AAA", "BBB"]

    def test_percentage_of_base_salary_ignores_proration(self) -> None:
        """A 15% allowance should not shrink because somebody took a day off."""
        result = calculate(
            payroll_input(
                component(
                    "HOUSING",
                    SalaryComponentType.EARNING,
                    CalculationType.PERCENTAGE,
                    percentage=Decimal("15"),
                ),
                attendance=AttendanceSummary(
                    working_days=Decimal("22"), payable_days=Decimal("11")
                ),
            )
        )
        housing = next(line for line in result.lines if line.component_code == "HOUSING")
        assert housing.amount == Decimal("600.0000")


class TestProration:
    def test_half_a_month_halves_the_basic(self) -> None:
        result = calculate(
            payroll_input(
                attendance=AttendanceSummary(working_days=Decimal("22"), payable_days=Decimal("11"))
            )
        )
        assert result.gross_salary == Decimal("2000.0000")

    def test_proration_can_be_switched_off_per_structure(self) -> None:
        result = calculate(
            payroll_input(
                attendance=AttendanceSummary(
                    working_days=Decimal("22"), payable_days=Decimal("11")
                ),
                prorate=False,
            )
        )
        assert result.gross_salary == Decimal("4000.0000")

    def test_factor_is_clamped_to_one(self) -> None:
        """Bad upstream data must never pay somebody more than their salary."""
        result = calculate(
            payroll_input(
                attendance=AttendanceSummary(working_days=Decimal("22"), payable_days=Decimal("30"))
            )
        )
        assert result.gross_salary == Decimal("4000.0000")

    def test_zero_working_days_does_not_divide_by_zero(self) -> None:
        result = calculate(
            payroll_input(
                attendance=AttendanceSummary(working_days=Decimal("0"), payable_days=Decimal("0"))
            )
        )
        assert result.gross_salary == Decimal("4000.0000")


class TestFormulas:
    def test_overtime_formula_uses_the_derived_hourly_rate(self) -> None:
        result = calculate(
            payroll_input(
                component(
                    "OVERTIME",
                    SalaryComponentType.EARNING,
                    CalculationType.FORMULA,
                    formula_key="overtime_hourly_1_5x",
                ),
                attendance=AttendanceSummary(
                    working_days=Decimal("22"),
                    payable_days=Decimal("22"),
                    overtime_minutes=120,
                ),
            )
        )
        # 4000 / 22 / 8 = 22.7272… per hour; ×1.5 ×2h = 68.18
        overtime = next(line for line in result.lines if line.component_code == "OVERTIME")
        assert overtime.amount == Decimal("68.1800")

    def test_no_overtime_worked_yields_zero(self) -> None:
        result = calculate(
            payroll_input(
                component(
                    "OVERTIME",
                    SalaryComponentType.EARNING,
                    CalculationType.FORMULA,
                    formula_key="overtime_hourly_1_5x",
                )
            )
        )
        overtime = next(line for line in result.lines if line.component_code == "OVERTIME")
        assert overtime.amount == Decimal("0.0000")

    def test_unknown_formula_is_rejected(self) -> None:
        from app.domain.payroll.exceptions import UnknownFormulaError

        with pytest.raises(UnknownFormulaError):
            calculate(
                payroll_input(
                    component(
                        "MYSTERY",
                        SalaryComponentType.EARNING,
                        CalculationType.FORMULA,
                        formula_key="does_not_exist",
                    )
                )
            )


class TestRejections:
    def test_negative_net_is_refused_not_clamped(self) -> None:
        """Silently paying zero would hide a configuration error for months."""
        with pytest.raises(NegativeNetPayError) as excinfo:
            calculate(
                payroll_input(
                    component(
                        "HUGE",
                        SalaryComponentType.DEDUCTION,
                        CalculationType.FIXED,
                        amount=Decimal("9999.00"),
                    )
                )
            )
        assert excinfo.value.code == "NEGATIVE_NET_PAY"

    def test_percentage_component_without_a_percentage_is_rejected(self) -> None:
        with pytest.raises(SalaryComponentInvalidError):
            calculate(
                payroll_input(
                    component("BROKEN", SalaryComponentType.EARNING, CalculationType.PERCENTAGE)
                )
            )

    def test_formula_component_without_a_key_is_rejected(self) -> None:
        with pytest.raises(SalaryComponentInvalidError):
            calculate(
                payroll_input(
                    component("BROKEN", SalaryComponentType.EARNING, CalculationType.FORMULA)
                )
            )


class TestDeterminism:
    def test_identical_input_produces_identical_output(self) -> None:
        """Golden Rule 4. Recalculating an old period must reproduce it."""
        components = (
            component(
                "HOUSING",
                SalaryComponentType.EARNING,
                CalculationType.PERCENTAGE,
                sequence=10,
                percentage=Decimal("15"),
            ),
            component(
                "PENSION",
                SalaryComponentType.DEDUCTION,
                CalculationType.PERCENTAGE,
                sequence=40,
                percentage=Decimal("5"),
                base=PercentageBase.GROSS_EARNINGS,
            ),
        )
        first = calculate(payroll_input(*components))
        second = calculate(payroll_input(*components))
        assert first.gross_salary == second.gross_salary
        assert first.net_salary == second.net_salary
        assert [line.amount for line in first.lines] == [line.amount for line in second.lines]

    def test_snapshot_records_the_inputs(self) -> None:
        """Golden Rule 6: the payslip must explain itself years later."""
        result = calculate(
            payroll_input(
                attendance=AttendanceSummary(working_days=Decimal("22"), payable_days=Decimal("20"))
            )
        )
        assert result.snapshot["contractual_base_salary"] == "4000.00"
        assert result.snapshot["working_days"] == "22"
        assert result.snapshot["payable_days"] == "20"
        assert result.snapshot["currency_code"] == "GBP"


class TestCurrencyScale:
    def test_zero_decimal_currency_rounds_to_whole_units(self) -> None:
        """JPY has no minor unit. Paying ¥1000.50 is not a thing."""
        result = calculate(
            payroll_input(
                component(
                    "HOUSING",
                    SalaryComponentType.EARNING,
                    CalculationType.PERCENTAGE,
                    percentage=Decimal("15.5"),
                ),
                base_salary=Decimal("333333"),
                decimal_places=0,
            )
        )
        housing = next(line for line in result.lines if line.component_code == "HOUSING")
        assert housing.amount == housing.amount.to_integral_value()


class TestMoneyPrimitives:
    def test_percentage_rounds_once_at_the_end(self) -> None:
        assert percentage_of(Decimal("3333.33"), Decimal("15")) == Decimal("500.0000")

    def test_allocate_never_loses_or_invents_a_cent(self) -> None:
        """The property that keeps split components reconciling exactly."""
        shares = allocate(Decimal("100.00"), [Decimal("1"), Decimal("1"), Decimal("1")])
        assert sum(shares) == Decimal("100.0000")
        assert shares == [Decimal("33.3400"), Decimal("33.3300"), Decimal("33.3300")]

    def test_allocate_handles_zero_weights(self) -> None:
        shares = allocate(Decimal("50.00"), [Decimal("0"), Decimal("0")])
        assert sum(shares) == Decimal("50.0000")

    def test_quantize_currency_is_exact(self) -> None:
        assert quantize_currency(Decimal("115.005"), 2) == Decimal("115.0100")

    def test_float_is_refused(self) -> None:
        from app.core.money import MoneyError, to_decimal

        with pytest.raises(MoneyError):
            to_decimal(1.5)  # type: ignore[arg-type]

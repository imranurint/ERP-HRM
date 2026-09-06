"""Registered payroll formulas.

A FORMULA salary component names a calculator here by `formula_key`. It does
**not** carry an expression string.

That is a deliberate and load-bearing decision. An expression column would
mean either `eval` — arbitrary code execution driven by a database row — or a
bespoke mini-language with its own parser, its own bugs, and no type checking.
Neither is acceptable for something that decides what people get paid.

A registered formula is ordinary Python: reviewed, unit-tested, type-checked,
and versioned with the rest of the codebase. Adding a country's tax band is a
pull request, which is exactly the amount of ceremony that change deserves.

Every formula must be a **pure function of its inputs**. No clocks, no random
values, no database access, no I/O. Payroll Golden Rule 4 (determinism) is
enforced here: recalculating an old period must reproduce the old numbers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.core.money import ZERO, percentage_of, quantize_currency
from app.modules.payroll.domain.exceptions import UnknownFormulaError


@dataclass(frozen=True, slots=True)
class FormulaContext:
    """Everything a formula is allowed to see.

    Anything not on this object is deliberately out of reach — a formula that
    needed a database lookup would no longer be deterministic.
    """

    base_salary: Decimal
    gross_earnings: Decimal
    taxable_earnings: Decimal
    total_deductions: Decimal
    working_days: Decimal
    payable_days: Decimal
    overtime_minutes: int
    currency_decimal_places: int = 2

    def money(self, value: Decimal) -> Decimal:
        return quantize_currency(value, self.currency_decimal_places)


FormulaFn = Callable[[FormulaContext], Decimal]

_REGISTRY: dict[str, FormulaFn] = {}
_DESCRIPTIONS: dict[str, str] = {}


def register(key: str, description: str) -> Callable[[FormulaFn], FormulaFn]:
    """Register a formula under a stable key.

    The key is what a `salary_components` row stores, so renaming one is a
    data migration. Treat these as a published contract.
    """

    def decorator(fn: FormulaFn) -> FormulaFn:
        if key in _REGISTRY:  # pragma: no cover - import-time programming error
            raise RuntimeError(f"payroll formula {key!r} is already registered")
        _REGISTRY[key] = fn
        _DESCRIPTIONS[key] = description
        return fn

    return decorator


def resolve(key: str) -> FormulaFn:
    formula = _REGISTRY.get(key)
    if formula is None:
        raise UnknownFormulaError(
            f"No payroll formula is registered under {key!r}.",
            details={"formula_key": key, "registered": sorted(_REGISTRY)},
        )
    return formula


def registered_formulas() -> Mapping[str, str]:
    """`{key: description}` — surfaced by the API so HR can pick from a list."""
    return dict(_DESCRIPTIONS)


# --------------------------------------------------------------------------
# The shipped catalogue.
#
# These are illustrative but real: each is a rule a company somewhere applies.
# A deployment is expected to add its own jurisdiction's rules alongside them.
# --------------------------------------------------------------------------
@register("overtime_hourly_1_5x", "Overtime at 1.5× the hourly rate derived from base salary")
def _overtime_1_5x(ctx: FormulaContext) -> Decimal:
    """Time-and-a-half on minutes worked beyond the shift.

    The hourly rate is derived from base salary over the standard working
    month, so it moves with a pay rise without anybody re-keying a rate.
    """
    if ctx.overtime_minutes <= 0 or ctx.working_days <= 0:
        return ZERO
    daily_rate = ctx.base_salary / ctx.working_days
    hourly_rate = daily_rate / Decimal("8")
    return ctx.money(hourly_rate * Decimal("1.5") * Decimal(ctx.overtime_minutes) / Decimal("60"))


@register("absence_deduction_daily", "Deduct unworked days at the standard daily rate")
def _absence_deduction(ctx: FormulaContext) -> Decimal:
    """The mirror image of proration, as an explicit deduction line.

    Some companies prefer the payslip to show full salary and a visible
    absence deduction rather than a quietly reduced basic.
    """
    if ctx.working_days <= 0:
        return ZERO
    unworked = ctx.working_days - ctx.payable_days
    if unworked <= 0:
        return ZERO
    return ctx.money(ctx.base_salary / ctx.working_days * unworked)


@register("progressive_tax_3_band", "Illustrative 3-band progressive tax on taxable earnings")
def _progressive_tax(ctx: FormulaContext) -> Decimal:
    """A worked example of banded tax.

    Bands are hardcoded *on purpose*: real jurisdictions differ, and a
    deployment replaces this with its own registered formula rather than
    trying to express its tax code in configuration rows.
    """
    bands: tuple[tuple[Decimal, Decimal], ...] = (
        (Decimal("1000"), Decimal("0")),
        (Decimal("3000"), Decimal("10")),
        (Decimal("999999999"), Decimal("20")),
    )
    remaining = ctx.taxable_earnings
    previous_ceiling = ZERO
    tax = ZERO
    for ceiling, rate in bands:
        if remaining <= 0:
            break
        slice_width = min(remaining, ceiling - previous_ceiling)
        tax += percentage_of(slice_width, rate, ctx.currency_decimal_places)
        remaining -= slice_width
        previous_ceiling = ceiling
    return ctx.money(tax)

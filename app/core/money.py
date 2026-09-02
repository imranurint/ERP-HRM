"""Monetary primitives.

Rules enforced here, once, for the whole system:

* money is `Decimal`, never `float`;
* storage scale is 4 (`NUMERIC(19,4)`);
* presentation/validation scale is the currency's `decimal_places`;
* rounding mode is configurable, default `ROUND_HALF_UP`.

Payroll depends on this being exact. A salary of 3333.33 split across
components must reassemble to 3333.33 every single run, on every machine, for
the rest of the company's life — which binary floating point cannot promise.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, DecimalException, InvalidOperation

from app.core.config import settings

#: Storage scale of every monetary column — NUMERIC(19, 4).
STORAGE_SCALE = 4
STORAGE_PRECISION = 19
STORAGE_QUANT = Decimal("0.0001")

ZERO = Decimal("0.0000")

#: Percentages are stored as NUMERIC(9, 4), i.e. 0.0000–100.0000 with 4 dp.
PERCENTAGE_PRECISION = 9
PERCENTAGE_SCALE = 4

#: Largest absolute value NUMERIC(19,4) can hold.
MAX_AMOUNT = Decimal("999999999999999.9999")

HUNDRED = Decimal("100")


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as a monetary amount."""


def to_decimal(value: str | int | Decimal) -> Decimal:
    """Parse an API/monetary input into `Decimal` without ever touching float."""
    if isinstance(value, float):  # pragma: no cover - defensive, typing forbids it
        raise MoneyError("float is not an acceptable monetary type")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, DecimalException, ArithmeticError) as exc:
        raise MoneyError(f"invalid monetary amount: {value!r}") from exc
    if not parsed.is_finite():
        raise MoneyError(f"invalid monetary amount: {value!r}")
    return parsed


def quantize_storage(value: Decimal) -> Decimal:
    """Round to the storage scale (4 dp) using the configured rounding mode."""
    return value.quantize(STORAGE_QUANT, rounding=settings.rounding_mode)


def quantize_currency(value: Decimal, decimal_places: int) -> Decimal:
    """Round to a currency's own scale, then widen back to storage scale."""
    if decimal_places < 0 or decimal_places > STORAGE_SCALE:
        raise MoneyError(f"unsupported currency scale: {decimal_places}")
    quant = Decimal(1).scaleb(-decimal_places)
    return quantize_storage(value.quantize(quant, rounding=settings.rounding_mode))


def exceeds_currency_precision(value: Decimal, decimal_places: int) -> bool:
    """True when `value` carries more decimals than the currency permits.

    We reject rather than silently round: a caller sending "115.005" to a
    2-decimal currency has a bug we must not paper over.
    """
    exponent = value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):  # NaN/Infinity guard
        return True
    return -exponent > decimal_places


def within_storage_range(value: Decimal) -> bool:
    return -MAX_AMOUNT <= value <= MAX_AMOUNT


def format_money(value: Decimal, decimal_places: int = 2) -> str:
    """Serialise money for transport: always a string, always fixed scale."""
    quant = Decimal(1).scaleb(-decimal_places)
    return str(value.quantize(quant, rounding=settings.rounding_mode))


def percentage_of(base: Decimal, percentage: Decimal, decimal_places: int = 2) -> Decimal:
    """`base * percentage / 100`, rounded once to the currency scale.

    Rounding happens exactly once, at the end. Rounding the intermediate would
    compound error across the dozen components a payslip can carry.
    """
    return quantize_currency(base * percentage / HUNDRED, decimal_places)


def allocate(total: Decimal, weights: Sequence[Decimal], decimal_places: int = 2) -> list[Decimal]:
    """Split `total` in proportion to `weights` with no lost or invented cents.

    Each share is rounded down to the currency scale, then the remainder is
    handed out one minor unit at a time to the largest fractional parts. The
    result always sums to exactly `total` — which is what keeps
    `SUM(payslip_items) == payslip.gross_salary` true by construction.
    """
    if not weights:
        return []

    quant = Decimal(1).scaleb(-decimal_places)
    minor = quant
    total_weight = sum(weights, start=Decimal(0))
    if total_weight == 0:
        # Nothing to weight by: give everything to the first share so the
        # invariant "shares sum to total" still holds.
        return [quantize_storage(total)] + [ZERO] * (len(weights) - 1)

    exact = [total * weight / total_weight for weight in weights]
    floored = [value.quantize(quant, rounding="ROUND_FLOOR") for value in exact]

    remainder = (total.quantize(quant, rounding=settings.rounding_mode) - sum(floored)) / minor
    units = int(remainder.to_integral_value(rounding="ROUND_HALF_UP"))

    order = sorted(
        range(len(weights)),
        key=lambda i: (exact[i] - floored[i], weights[i]),
        reverse=True,
    )
    for position in range(units):
        floored[order[position % len(order)]] += minor

    return [quantize_storage(value) for value in floored]

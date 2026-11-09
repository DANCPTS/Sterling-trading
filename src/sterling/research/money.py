"""Decimal helpers for the research simulator.

GBP cash is rounded half-even to pence. Floats are rejected.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation

from sterling.research.errors import ResearchError

MONEY_QUANTUM = Decimal("0.01")
QTY_QUANTUM = Decimal("0.00000001")
WEIGHT_QUANTUM = Decimal("0.00000001")
ZERO = Decimal("0")
ZERO_MONEY = Decimal("0.00")


def parse_decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ResearchError(f"{label} must be a decimal string, not {type(value).__name__}")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, str):
        if "e" in value.lower():
            raise ResearchError(f"{label} must not use exponential notation")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ResearchError(f"{label} is not a decimal") from exc
    else:
        raise ResearchError(f"{label} must be a decimal string, not {type(value).__name__}")
    if not amount.is_finite():
        raise ResearchError(f"{label} must be finite")
    return amount


def quantize_money(amount: Decimal) -> Decimal:
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized == 0:
        return ZERO_MONEY
    return quantized


def quantize_qty_down(amount: Decimal) -> Decimal:
    if amount < 0:
        raise ResearchError("quantity cannot be negative")
    return amount.quantize(QTY_QUANTUM, rounding=ROUND_DOWN)


def quantize_weight_down(amount: Decimal) -> Decimal:
    return amount.quantize(WEIGHT_QUANTUM, rounding=ROUND_DOWN)

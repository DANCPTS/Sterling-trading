"""Decimal money helpers.

GBP cash is quantized to pence with half-even rounding at each posting
boundary. Floats are rejected so binary fractions cannot enter the ledger.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from sterling.errors import MoneyError

MONEY_QUANTUM = Decimal("0.01")
MAX_INPUT_SCALE = 8
ROUNDING = ROUND_HALF_EVEN

_ZERO = Decimal("0.00")


def _parse(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyError(f"{label} must be Decimal or str, not {type(value).__name__}")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, str):
        if "e" in value.lower():
            raise MoneyError(f"{label} must not use exponential notation")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise MoneyError(f"{label} is not a decimal") from exc
    else:
        raise MoneyError(f"{label} must be Decimal or str, not {type(value).__name__}")
    if not amount.is_finite():
        raise MoneyError(f"{label} must be a finite decimal")
    return amount


def quantize_money(amount: Decimal) -> Decimal:
    """Round a Decimal to pence, half-even. Zero is normalized to 0.00."""
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUNDING)
    if quantized == 0:
        return _ZERO
    return quantized


def require_scale(value: object, *, label: str, places: int = MAX_INPUT_SCALE) -> Decimal:
    amount = _parse(value, label)
    quantum = Decimal(1).scaleb(-places)
    if amount != amount.quantize(quantum):
        raise MoneyError(f"{label} has more than {places} decimal places")
    return amount


def require_money(value: object, *, label: str = "money") -> Decimal:
    """Accept a value already on the penny grid and return it quantized."""
    amount = require_scale(value, label=label, places=2)
    return quantize_money(amount)


def require_positive_money(value: object, *, label: str) -> Decimal:
    amount = require_money(value, label=label)
    if amount <= 0:
        raise MoneyError(f"{label} must be positive")
    return amount


def require_non_negative_money(value: object, *, label: str) -> Decimal:
    amount = require_money(value, label=label)
    if amount < 0:
        raise MoneyError(f"{label} must be >= 0")
    return amount


def signed_money(value: object, *, label: str) -> Decimal:
    """Parse a penny amount that may be negative, such as realised P&L."""
    amount = require_scale(value, label=label, places=2)
    return quantize_money(amount)


def require_positive_quantity(value: object, *, label: str) -> Decimal:
    amount = require_scale(value, label=label)
    if amount <= 0:
        raise MoneyError(f"{label} must be positive")
    return amount


def require_non_negative_price(value: object, *, label: str) -> Decimal:
    amount = require_scale(value, label=label)
    if amount < 0:
        raise MoneyError(f"{label} must be >= 0")
    return amount


def require_positive_price(value: object, *, label: str) -> Decimal:
    amount = require_scale(value, label=label)
    if amount <= 0:
        raise MoneyError(f"{label} must be positive")
    return amount


def require_fx_rate(value: object, *, label: str = "fx_rate_to_gbp") -> Decimal:
    amount = require_scale(value, label=label)
    if amount <= 0:
        raise MoneyError(f"{label} must be positive")
    return amount


def gbp_notional(quantity: Decimal, price: Decimal, fx_rate_to_gbp: Decimal) -> Decimal:
    """Convert an instrument-currency consideration to pence, once."""
    return quantize_money(quantity * price * fx_rate_to_gbp)


def money_to_str(amount: Decimal) -> str:
    return format(quantize_money(amount), "f")


def decimal_to_str(amount: Decimal) -> str:
    return format(amount, "f")

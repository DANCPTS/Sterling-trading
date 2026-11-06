"""Pure risk-budget arithmetic from loaded rules.

These figures are budgets. Stage 1 does not enforce them. The per-trade budget
already includes expected costs and an adverse-execution allowance; those are
not added on again.
"""

from __future__ import annotations

from decimal import Decimal

from sterling.errors import MoneyError
from sterling.ledger.money import quantize_money, require_money
from sterling.rules.model import DeskRules


def _equity(value: object, label: str) -> Decimal:
    amount = require_money(value, label=label)
    if amount < 0:
        raise MoneyError(f"{label} must be >= 0")
    return amount


def per_trade_planned_risk(equity: object, rules: DeskRules) -> Decimal:
    """min(0.5% of current equity, the 2.50 GBP absolute ceiling), in pence."""
    current = _equity(equity, "equity")
    raw = current * rules.per_trade_equity_fraction
    return quantize_money(min(raw, rules.per_trade_absolute_ceiling))


def daily_loss_threshold(day_opening_equity: object, rules: DeskRules) -> Decimal:
    """min(1% of day-opening equity, the 5.00 GBP absolute ceiling), in pence."""
    opening = _equity(day_opening_equity, "day_opening_equity")
    raw = opening * rules.daily_loss_equity_fraction
    return quantize_money(min(raw, rules.daily_loss_absolute_ceiling))


def total_open_planned_risk_cap(equity: object, rules: DeskRules) -> Decimal:
    """1% of equity. No separate absolute ceiling is defined for this cap."""
    current = _equity(equity, "equity")
    return quantize_money(current * rules.total_open_planned_risk_equity_fraction)

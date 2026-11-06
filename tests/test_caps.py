"""Hand-checked risk budgets. These are ceilings, not performance."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sterling.errors import MoneyError
from sterling.rules.caps import (
    daily_loss_threshold,
    per_trade_planned_risk,
    total_open_planned_risk_cap,
)
from sterling.rules.model import DeskRules

# equity, per-trade, daily loss threshold, open planned-risk cap
_CASES = [
    ("0.00", "0.00", "0.00", "0.00"),
    ("100.00", "0.50", "1.00", "1.00"),
    ("250.00", "1.25", "2.50", "2.50"),
    ("400.00", "2.00", "4.00", "4.00"),
    ("497.00", "2.48", "4.97", "4.97"),
    ("499.00", "2.50", "4.99", "4.99"),
    ("500.00", "2.50", "5.00", "5.00"),
    ("600.00", "2.50", "5.00", "6.00"),
    ("333.33", "1.67", "3.33", "3.33"),
    ("100000.00", "2.50", "5.00", "1000.00"),
]


@pytest.mark.parametrize(("equity", "per_trade", "daily", "open_risk"), _CASES)
def test_cap_arithmetic(
    equity: str,
    per_trade: str,
    daily: str,
    open_risk: str,
    rules: DeskRules,
) -> None:
    assert per_trade_planned_risk(equity, rules) == Decimal(per_trade)
    assert daily_loss_threshold(equity, rules) == Decimal(daily)
    assert total_open_planned_risk_cap(equity, rules) == Decimal(open_risk)
    assert per_trade_planned_risk(equity, rules) <= Decimal("2.50")
    assert daily_loss_threshold(equity, rules) <= Decimal("5.00")


def test_primary_account_hits_both_absolute_ceilings(rules: DeskRules) -> None:
    assert per_trade_planned_risk(Decimal("500.00"), rules) == Decimal("2.50")
    assert daily_loss_threshold(Decimal("500.00"), rules) == Decimal("5.00")
    assert total_open_planned_risk_cap(Decimal("500.00"), rules) == Decimal("5.00")


def test_float_and_negative_equity_are_rejected(rules: DeskRules) -> None:
    with pytest.raises(MoneyError):
        per_trade_planned_risk(500.0, rules)
    with pytest.raises(MoneyError):
        daily_loss_threshold("-1.00", rules)

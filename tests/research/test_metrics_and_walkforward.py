"""Metrics on a known NAV path, and causal walk-forward splits."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from sterling.research.errors import ResearchError
from sterling.research.metrics import METRIC_QUANTUM, NavRow, performance
from sterling.research.money import ZERO
from sterling.research.walkforward import DateSplit, assert_causal_split, walk_forward_splits


def _row(day: date, nav: str, cash: str = "0") -> NavRow:
    nav_value = Decimal(nav)
    cash_value = Decimal(cash)
    market = nav_value - cash_value
    gross = Decimal(0) if nav_value == 0 else market / nav_value
    return NavRow(day, cash_value, market, nav_value, gross, cash_value / nav_value)


def test_metrics_on_a_hand_built_path() -> None:
    rows = (
        _row(date(2024, 1, 31), "100", "0"),
        _row(date(2024, 2, 10), "120", "0"),
        _row(date(2024, 2, 20), "90", "0"),
        _row(date(2024, 2, 29), "100", "20"),
        _row(date(2024, 3, 10), "130", "0"),
    )
    # Daily returns from 100: +0.2, -0.25, +1/9, +0.3
    result = performance(rows, total_cost_gbp=Decimal("2.00"), total_turnover=Decimal("0.5"))
    assert result.max_drawdown == ((Decimal("120") - Decimal("90")) / Decimal("120")).quantize(METRIC_QUANTUM)
    assert result.max_drawdown_duration_days == 10
    assert result.max_drawdown_peak == date(2024, 2, 10)
    assert result.max_drawdown_trough == date(2024, 2, 20)
    assert result.max_drawdown_underwater_days == 29
    assert result.end_nav_gbp == Decimal("130")
    returns = [Decimal("0.2"), Decimal("-0.25"), Decimal("100") / Decimal("90") - 1, Decimal("0.3")]
    mean = sum(returns, ZERO) / Decimal(len(returns))
    variance = sum(((item - mean) ** 2 for item in returns), ZERO) / Decimal(len(returns) - 1)
    expected_vol = (variance.sqrt() * Decimal(252).sqrt()).quantize(METRIC_QUANTUM)
    assert result.annualised_volatility == expected_vol
    assert result.monthly_win_rate == Decimal("1")
    assert result.total_cost_gbp == Decimal("2.00")
    assert result.total_cost_drag == (Decimal("2.00") / Decimal("100")).quantize(METRIC_QUANTUM)
    assert result.average_cash_weight is not None
    assert result.fraction_days_in_cash == 0


def test_flat_nav_has_zero_volatility_and_no_sharpe() -> None:
    start = date(2024, 1, 1)
    rows = tuple(_row(start + timedelta(days=offset), "100", "100") for offset in range(5))
    result = performance(rows, total_cost_gbp=Decimal("0.00"), total_turnover=Decimal("0"))
    assert result.annualised_volatility == 0
    assert result.sharpe is None
    assert result.max_drawdown == 0
    assert result.cagr == 0
    assert result.fraction_days_in_cash == 1


def test_walk_forward_splits_do_not_overlap() -> None:
    splits = walk_forward_splits(date(2023, 10, 2), date(2024, 5, 31), train_months=4, test_months=3)
    assert splits
    for split in splits:
        assert_causal_split(split)
        assert split.test_start == split.train_end + timedelta(days=1)
    overlap = DateSplit(date(2024, 1, 1), date(2024, 4, 1), date(2024, 4, 1), date(2024, 6, 1))
    with pytest.raises(ResearchError, match="looks ahead"):
        assert_causal_split(overlap)

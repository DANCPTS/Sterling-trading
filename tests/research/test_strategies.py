"""Known strategy signals on synthetic month-end closes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sterling.research.strategies.dual_momentum import DualMomentum
from sterling.research.strategies.trend import TrendFilter
from sterling.research.strategy import MarketView, default_registry


def _view(as_of: date, series: dict[str, tuple[tuple[date, Decimal], ...]]) -> MarketView:
    return MarketView(as_of=as_of, closes=series)


def test_trend_filter_matches_a_three_month_average() -> None:
    points = (
        (date(2020, 1, 31), Decimal("10")),
        (date(2020, 2, 28), Decimal("10")),
        (date(2020, 3, 31), Decimal("10")),
        (date(2020, 4, 30), Decimal("12")),
        (date(2020, 5, 29), Decimal("9")),
    )
    strategy = TrendFilter(lookback_months=3)
    cash = strategy.target_weights(_view(date(2020, 3, 31), {"A": points[:3]}), ["A"])
    assert cash == {}
    invested = strategy.target_weights(_view(date(2020, 4, 30), {"A": points[:4]}), ["A"])
    assert invested == {"A": Decimal("1")}
    # May close 9 is below (10 + 12 + 9) / 3.
    out = strategy.target_weights(_view(date(2020, 5, 29), {"A": points}), ["A"])
    assert out == {}


def test_trend_equal_weights_only_names_above_the_average() -> None:
    as_of = date(2020, 3, 31)
    view = _view(
        as_of,
        {
            "A": (
                (date(2020, 1, 31), Decimal("10")),
                (date(2020, 2, 28), Decimal("10")),
                (date(2020, 3, 31), Decimal("13")),
            ),
            "B": (
                (date(2020, 1, 31), Decimal("10")),
                (date(2020, 2, 28), Decimal("10")),
                (date(2020, 3, 31), Decimal("10")),
            ),
        },
    )
    weights = TrendFilter(lookback_months=3).target_weights(view, ["A", "B"])
    assert weights == {"A": Decimal("1")}


def test_dual_momentum_ranks_and_drops_non_positive_returns() -> None:
    as_of = date(2020, 2, 28)
    view = _view(
        as_of,
        {
            "AAA": ((date(2020, 1, 31), Decimal("100")), (date(2020, 2, 28), Decimal("110"))),
            "BBB": ((date(2020, 1, 31), Decimal("100")), (date(2020, 2, 28), Decimal("105"))),
            "CCC": ((date(2020, 1, 31), Decimal("100")), (date(2020, 2, 28), Decimal("90"))),
        },
    )
    top = DualMomentum(lookback_months=1, top_k=1).target_weights(view, ["CCC", "BBB", "AAA"])
    assert top == {"AAA": Decimal("1")}
    two = DualMomentum(lookback_months=1, top_k=2).target_weights(view, ["AAA", "BBB", "CCC"])
    assert two == {"AAA": Decimal("0.5"), "BBB": Decimal("0.5")}
    high_floor = DualMomentum(lookback_months=1, top_k=2, absolute_floor=Decimal("0.08")).target_weights(
        view, ["AAA", "BBB", "CCC"]
    )
    assert high_floor == {"AAA": Decimal("1")}
    losing = _view(
        as_of,
        {
            "AAA": ((date(2020, 1, 31), Decimal("100")), (date(2020, 2, 28), Decimal("90"))),
            "BBB": ((date(2020, 1, 31), Decimal("100")), (date(2020, 2, 28), Decimal("80"))),
        },
    )
    assert DualMomentum(lookback_months=1, top_k=1).target_weights(losing, ["AAA", "BBB"]) == {}


def test_dual_momentum_tie_breaks_on_ticker() -> None:
    as_of = date(2020, 2, 28)
    view = _view(
        as_of,
        {
            "BBB": ((date(2020, 1, 31), Decimal("100")), (date(2020, 2, 28), Decimal("110"))),
            "AAA": ((date(2020, 1, 31), Decimal("100")), (date(2020, 2, 28), Decimal("110"))),
        },
    )
    assert DualMomentum(lookback_months=1, top_k=1).target_weights(view, ["BBB", "AAA"]) == {"AAA": Decimal("1")}


def test_registry_reserves_later_strategies() -> None:
    registry = default_registry()
    assert "trend_filter" in registry.names()
    assert "dual_momentum" in registry.names()
    with pytest.raises(Exception, match="reserved"):
        registry.create("fx_research")
    with pytest.raises(Exception, match="reserved"):
        registry.create("breakout")

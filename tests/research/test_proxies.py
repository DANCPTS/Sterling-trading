"""Splices, sterling conversion, and the pre-set no-trade band."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sterling.research.costs import research_cost_model
from sterling.research.proxies import (
    compound_annual_percent,
    distribution_total_return,
    drop_isolated_spikes,
    hedged_return_index,
    ratio_splice,
    usd_price_to_gbp,
)
from sterling.research.risk_config import ResearchLimits
from sterling.research.risk_gate import apply_risk_gate


def _limits() -> ResearchLimits:
    return ResearchLimits(
        status="APPROVED FOR RESEARCH SIMULATION ONLY by owner 2026-11-09; not operational",
        owner_approved=False,
        long_only=True,
        max_gross=Decimal("1"),
        max_single_instrument_weight=Decimal("1"),
        max_asset_class_weight=(("equity", Decimal("1")), ("cash_proxy", Decimal("1"))),
        cash_min=Decimal("0"),
        cash_max=Decimal("1"),
        min_trade_notional_gbp=Decimal("0"),
        max_one_way_turnover=Decimal("1"),
        rebalance="monthly",
        starting_nav_gbp=Decimal("10000"),
        notes=(),
    )


def test_ratio_splice_scales_history_and_drops_later_proxy_points() -> None:
    proxy = (
        (date(2001, 1, 1), Decimal("2")),
        (date(2001, 1, 2), Decimal("4")),
        (date(2001, 1, 3), Decimal("8")),
    )
    actual = (
        (date(2001, 1, 2), Decimal("10")),
        (date(2001, 1, 3), Decimal("11")),
    )
    spliced = ratio_splice(proxy, actual, date(2001, 1, 2))
    assert spliced == (
        (date(2001, 1, 1), Decimal("5"), "proxy"),
        (date(2001, 1, 2), Decimal("10"), "actual"),
        (date(2001, 1, 3), Decimal("11"), "actual"),
    )


def test_usd_price_uses_dollars_per_pound_and_a_hedge_does_not() -> None:
    assert usd_price_to_gbp(Decimal("125"), Decimal("1.25")) == Decimal("100")
    local = ((date(2020, 1, 1), Decimal("100")), (date(2020, 1, 2), Decimal("101")))
    domestic = ((date(2020, 1, 2), Decimal("1")),)
    foreign = ((date(2020, 1, 2), Decimal("1")),)
    hedged = hedged_return_index(local, domestic, foreign)
    # Equal rates, one day: index follows the 1% local gain and no FX multiplication.
    assert hedged[0] == (date(2020, 1, 1), Decimal("1"))
    assert hedged[1][1] == Decimal("1.01")
    assert hedged[1][1] != usd_price_to_gbp(Decimal("101"), Decimal("1.25"))


def test_rate_index_compounds_across_a_gap() -> None:
    levels = compound_annual_percent(((date(2020, 1, 1), Decimal("0")), (date(2020, 1, 3), Decimal("0"))))
    assert levels[0][1] == Decimal("1")
    assert levels[1][1] == Decimal("1")
    grown = compound_annual_percent(((date(2020, 1, 1), Decimal("365.25")), (date(2020, 1, 2), Decimal("365.25"))))
    # 365.25% over one day of a 365.25-day year is exactly +1%.
    assert grown[1][1] == Decimal("1.01")


def test_two_point_band_skips_drift_and_allows_a_real_exit() -> None:
    limits = _limits()
    classes = {"EQ": "equity", "CASH": "cash_proxy"}
    drift = apply_risk_gate(
        {"EQ": Decimal("0.51")},
        {"EQ": Decimal("0.50")},
        limits,
        classes,
        Decimal("10000"),
        no_trade_band=Decimal("0.02"),
    )
    assert drift.weight_map() == {"EQ": Decimal("0.50")}
    assert any("no_trade_band" in reason for reason in drift.reasons)
    exit_book = apply_risk_gate(
        {"CASH": Decimal("1")},
        {"EQ": Decimal("1")},
        limits,
        classes,
        Decimal("10000"),
        no_trade_band=Decimal("0.02"),
    )
    assert exit_book.weight_map() == {"CASH": Decimal("1")}


def test_accrual_index_counts_the_dividend_on_the_ex_date() -> None:
    levels = distribution_total_return(
        (
            (date(2024, 8, 20), Decimal("100"), Decimal("0")),
            (date(2024, 8, 21), Decimal("97.5"), Decimal("2.5")),
            (date(2024, 8, 22), Decimal("97.5"), Decimal("0")),
        )
    )
    assert [level for _day, level in levels] == [Decimal("100"), Decimal("100"), Decimal("100")]


def test_isolated_price_spikes_are_dropped_and_sustained_moves_stay() -> None:
    levels = (
        (date(2011, 4, 11), Decimal("30")),
        (date(2011, 4, 12), Decimal("18")),
        (date(2011, 4, 15), Decimal("30")),
        (date(2011, 4, 18), Decimal("29")),
    )
    cleaned = drop_isolated_spikes(levels)
    assert [level for _day, level in cleaned] == [Decimal("30"), Decimal("30"), Decimal("29")]


def test_crisis_and_early_spreads_were_fixed_before_the_long_run() -> None:
    calm = research_cost_model(date(2019, 6, 3))
    early = research_cost_model(date(2004, 6, 1))
    crash = research_cost_model(date(2020, 3, 16))
    assert calm.spread_bps == Decimal("10")
    assert calm.slippage_bps == Decimal("5")
    assert early.spread_bps == Decimal("25")
    assert early.slippage_bps == Decimal("10")
    assert crash.spread_bps == Decimal("10")
    assert crash.slippage_bps == Decimal("20")

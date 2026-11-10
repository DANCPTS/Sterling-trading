"""Costs, the research risk gate, and hard long-only / cash constraints."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sterling.research.costs import CostModel, affordable_buy, quote_trade
from sterling.research.engine import _Book, _distributions, _rebalance
from sterling.research.errors import ResearchError
from sterling.research.instruments import Instrument, gross_index_fee_drag
from sterling.research.money import ZERO_MONEY
from sterling.research.risk_config import ResearchLimits
from sterling.research.risk_gate import apply_risk_gate

DAY = date(2024, 1, 2)


def _limits(**overrides: object) -> ResearchLimits:
    values: dict[str, object] = {
        "status": "PROPOSED - pending owner approval",
        "owner_approved": False,
        "long_only": True,
        "max_gross": Decimal("1"),
        "max_single_instrument_weight": Decimal("1"),
        "max_asset_class_weight": (("equity", Decimal("1")), ("bond", Decimal("1"))),
        "cash_min": Decimal("0"),
        "cash_max": Decimal("1"),
        "min_trade_notional_gbp": Decimal("0"),
        "max_one_way_turnover": Decimal("1"),
        "rebalance": "monthly",
        "starting_nav_gbp": Decimal("10000.00"),
        "notes": (),
    }
    values.update(overrides)
    return ResearchLimits(**values)  # type: ignore[arg-type]


def test_stamp_spread_slippage_and_fx_costs() -> None:
    share = quote_trade(
        "BUY",
        Decimal("100"),
        Decimal("100"),
        CostModel(),
        stamp=True,
        charge_fx=False,
    )
    assert share.notional_gbp == Decimal("10000.00")
    assert share.stamp_duty_gbp == Decimal("50.00")
    etf = quote_trade(
        "BUY",
        Decimal("100"),
        Decimal("100"),
        CostModel(),
        stamp=False,
        charge_fx=False,
    )
    assert etf.stamp_duty_gbp == ZERO_MONEY
    sell = quote_trade("SELL", Decimal("100"), Decimal("100"), CostModel(), stamp=True, charge_fx=False)
    assert sell.stamp_duty_gbp == ZERO_MONEY
    bumped = quote_trade(
        "BUY",
        Decimal("10"),
        Decimal("100"),
        CostModel(spread_bps=Decimal("10"), slippage_bps=Decimal("5")),
        stamp=False,
        charge_fx=False,
    )
    assert bumped.exec_price_gbp == Decimal("100.1")
    assert bumped.spread_cost_gbp == Decimal("0.50")
    assert bumped.slippage_cost_gbp == Decimal("0.50")
    fx = quote_trade(
        "BUY",
        Decimal("100"),
        Decimal("100"),
        CostModel(fx_conversion_bps=Decimal("10")),
        stamp=False,
        charge_fx=True,
    )
    assert fx.fx_cost_gbp == Decimal("10.00")
    no_fx = quote_trade(
        "BUY",
        Decimal("100"),
        Decimal("100"),
        CostModel(fx_conversion_bps=Decimal("10")),
        stamp=False,
        charge_fx=False,
    )
    assert no_fx.fx_cost_gbp == ZERO_MONEY


def test_buy_sizing_keeps_cash_non_negative_when_stamp_is_due() -> None:
    quote = affordable_buy(
        Decimal("10000.00"),
        Decimal("100"),
        CostModel(),
        stamp=True,
        charge_fx=False,
    )
    assert quote.cash_delta_gbp >= Decimal("-10000.00")
    assert quote.stamp_duty_gbp > 0
    assert quote.notional_gbp + quote.stamp_duty_gbp <= Decimal("10000.00")


def test_risk_gate_vetoes_shorts_and_clips_size_class_and_turnover() -> None:
    limits = _limits(
        max_single_instrument_weight=Decimal("0.40"),
        max_asset_class_weight=(("equity", Decimal("0.80")), ("bond", Decimal("0.50"))),
        min_trade_notional_gbp=Decimal("25"),
        max_one_way_turnover=Decimal("1"),
    )
    classes = {"AAA": "equity", "BBB": "equity", "CCC": "bond"}
    veto = apply_risk_gate({"AAA": Decimal("-0.2")}, {}, limits, classes, Decimal("10000"))
    assert veto.veto is True
    assert veto.weights == ()
    assert any("long_only" in reason for reason in veto.reasons)
    clipped = apply_risk_gate({"AAA": Decimal("0.80")}, {}, limits, classes, Decimal("10000"))
    assert clipped.accepted is True
    assert clipped.weight_map() == {"AAA": Decimal("0.40")}
    both = apply_risk_gate(
        {"AAA": Decimal("0.50"), "BBB": Decimal("0.50")},
        {},
        limits,
        classes,
        Decimal("10000"),
    )
    # Single-name cap binds first (0.40 + 0.40), inside the 0.80 equity cap.
    assert both.weight_map()["AAA"] == Decimal("0.40")
    assert both.weight_map()["BBB"] == Decimal("0.40")
    crowded = apply_risk_gate(
        {"AAA": Decimal("0.40"), "BBB": Decimal("0.40"), "CCC": Decimal("0.10")},
        {},
        _limits(
            max_single_instrument_weight=Decimal("0.40"),
            max_asset_class_weight=(("equity", Decimal("0.50")), ("bond", Decimal("1"))),
        ),
        classes,
        Decimal("10000"),
    )
    equity_total = crowded.weight_map()["AAA"] + crowded.weight_map()["BBB"]
    assert equity_total <= Decimal("0.50")
    dust = apply_risk_gate({"AAA": Decimal("0.001")}, {}, limits, classes, Decimal("10000"))
    assert dust.weight_map() == {}
    turnover_limits = _limits(max_one_way_turnover=Decimal("0.10"))
    scaled = apply_risk_gate({"AAA": Decimal("1")}, {}, turnover_limits, {"AAA": "equity"}, Decimal("10000"))
    assert scaled.weight_map()["AAA"] == Decimal("0.2")


def test_execution_refuses_leverage_and_reinvests_a_distribution_once() -> None:
    instrument = Instrument(
        ticker="T",
        instrument_type="UCITS_ETF",
        product_class="ucits_etf_physical",
        asset_class="equity",
        trading_currency="GBP",
        stamp_duty_exempt=True,
        distribution="distributing",
        calendar_id="XLON_WEEKDAY",
    )
    book = _Book(cash=Decimal("100.00"), quantity={})
    with pytest.raises(ResearchError, match="leverage or a short"):
        _rebalance(
            book,
            {"T": Decimal("1.2")},
            {"T": Decimal("10")},
            {"T": instrument},
            CostModel(),
            Decimal("0"),
            DAY,
            None,
        )
    from sterling.research.bars import GbpBar, PreparedSeries

    prepared = PreparedSeries(
        "T",
        (
            GbpBar(
                session=DAY,
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                signal_close=Decimal("100"),
                volume=Decimal("1"),
                dividend=Decimal("2"),
            ),
        ),
        "distributions",
        "reinvest",
        (),
    )
    holding = _Book(cash=Decimal("0.00"), quantity={"T": Decimal("10")})
    from sterling.research.calendar import xlon

    _distributions(holding, {"T": prepared}, DAY, {"T": instrument}, CostModel(), xlon(), {"T": Decimal("100")})
    assert holding.quantity["T"] == Decimal("10")
    assert holding.cash == Decimal("0.00")
    assert holding.receivable() == Decimal("20.00")


def test_cash_proxy_can_be_fully_invested_and_a_risk_off_exit_is_not_scaled() -> None:
    limits = _limits(
        max_single_instrument_weight=Decimal("0.50"),
        max_asset_class_weight=(("equity", Decimal("0.80")), ("commodity", Decimal("0.20")), ("cash_proxy", Decimal("1"))),
        max_one_way_turnover=Decimal("0.50"),
        single_instrument_cap_exempt_asset_classes=("cash_proxy",),
        risk_off_turnover_exception=True,
    )
    classes = {"AAA": "equity", "GOLD": "commodity", "XSTR": "cash_proxy"}
    parked = apply_risk_gate({"XSTR": Decimal("1")}, {}, limits, classes, Decimal("10000"))
    assert parked.veto is False
    assert parked.weight_map() == {"XSTR": Decimal("1")}
    assert parked.cash_weight == 0
    exit_book = apply_risk_gate(
        {"XSTR": Decimal("1")},
        {"AAA": Decimal("0.60"), "GOLD": Decimal("0.20")},
        limits,
        classes,
        Decimal("10000"),
    )
    assert exit_book.weight_map() == {"XSTR": Decimal("1")}
    assert any("risk_off_turnover_exception" in reason for reason in exit_book.reasons)
    uncapped = _limits(
        max_single_instrument_weight=Decimal("1"),
        max_one_way_turnover=Decimal("0.50"),
        risk_off_turnover_exception=True,
    )
    rotated = apply_risk_gate(
        {"BBB": Decimal("1")},
        {"AAA": Decimal("1")},
        uncapped,
        {"AAA": "equity", "BBB": "equity"},
        Decimal("10000"),
    )
    assert rotated.weight_map()["AAA"] == Decimal("0.5")
    assert rotated.weight_map()["BBB"] == Decimal("0.5")
    assert any(reason.startswith("scaled turnover") for reason in rotated.reasons)


def test_gross_index_drag_is_not_the_exchange_price_path() -> None:
    assert gross_index_fee_drag(Decimal("36525.00"), Decimal("0.01")) == Decimal("1.00")

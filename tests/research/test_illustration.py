"""Worked synthetic example: next-open fills, cash, costs, and benchmarks."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sterling.research.bars import Bar
from sterling.research.calendar import xlon
from sterling.research.engine import build_view, simulate
from sterling.research.errors import ResearchError
from sterling.research.report import write_report
from sterling.research.risk_config import load_research_limits
from sterling.research.strategies.trend import TrendFilter
from sterling.research.synthetic import illustration_bars, illustration_costs, illustration_instruments, run_illustration
from sterling.research.walkforward import walk_forward_splits


def test_illustration_buys_then_sells_with_costs_and_benchmarks(tmp_path: object) -> None:
    from pathlib import Path

    result = run_illustration()
    buys = [fill for fill in result.fills if fill.side == "BUY" and fill.ticker == "SYNTH-EQ"]
    sells = [fill for fill in result.fills if fill.side == "SELL" and fill.ticker == "SYNTH-EQ"]
    assert len(buys) == 1
    assert len(sells) == 1
    assert buys[0].signal_date == date(2024, 1, 31)
    assert buys[0].session == date(2024, 2, 1)
    assert buys[0].mid_price_gbp == Decimal("130")
    assert sells[0].signal_date == date(2024, 3, 28)
    assert sells[0].session == date(2024, 4, 2)
    assert sells[0].mid_price_gbp == Decimal("80")
    assert buys[0].session != buys[0].signal_date
    assert all(fill.stamp_duty_gbp == 0 for fill in result.fills)
    assert all(fill.fx_cost_gbp == 0 for fill in result.fills)
    assert buys[0].commission_gbp == Decimal("1.00")
    assert buys[0].exec_price_gbp == Decimal("130.13")
    assert result.total_cost_gbp > 0
    assert result.ocf_deducted_gbp == 0
    assert all(row.cash_gbp >= 0 for row in result.nav)
    assert all(row.gross_exposure <= Decimal("1.00000001") for row in result.nav)
    january = next(item for item in result.decisions if item["signal_date"] == "2024-01-31")
    assert january["weights"] == {"SYNTH-EQ": "0.50000000"}
    assert january["data_cutoff"] == "2024-01-31"
    assert "max single instrument" in " ".join(january["reasons"])  # type: ignore[arg-type]
    names = [name for name, _metrics in result.benchmarks]
    assert names == ["buy_and_hold:SYNTH-EQ", "buy_and_hold:SYNTH-BOND", "equal_weight_monthly"]
    strategy_end = result.metrics.end_nav_gbp
    equity_end = result.benchmarks[0][1].end_nav_gbp
    assert strategy_end != equity_end
    directory = Path(str(tmp_path))
    write_report(result, directory)
    for name in ("nav.csv", "fills.csv", "metrics.json", "benchmarks.json", "proposals.jsonl", "summary.txt"):
        assert (directory / name).is_file()
    assert "cagr" in (directory / "summary.txt").read_text(encoding="utf-8")


def test_fill_uses_the_next_open_not_the_signal_close() -> None:
    equity, bond = illustration_instruments()
    bars = illustration_bars()
    replaced: list[Bar] = []
    for bar in bars["SYNTH-EQ"]:
        if bar.session == date(2024, 2, 1):
            replaced.append(
                Bar(
                    session=bar.session,
                    open=Decimal("110"),
                    high=Decimal("130"),
                    low=Decimal("110"),
                    close=Decimal("130"),
                    volume=bar.volume,
                    quoted_in="GBP",
                )
            )
        else:
            replaced.append(bar)
    bars = {"SYNTH-EQ": tuple(replaced), "SYNTH-BOND": bars["SYNTH-BOND"]}
    result = simulate(
        instruments=(equity, bond),
        bars=bars,
        calendar=xlon(),
        strategy=TrendFilter(lookback_months=3),
        limits=load_research_limits(),
        cost_model=illustration_costs(),
        label="next-open check",
    )
    buy = next(fill for fill in result.fills if fill.side == "BUY")
    assert buy.signal_date == date(2024, 1, 31)
    assert buy.mid_price_gbp == Decimal("110")
    assert buy.exec_price_gbp == Decimal("110.11")


def test_future_prices_do_not_change_the_signal_and_a_leaked_close_would() -> None:
    from sterling.research.bars import prepare_series

    equity, bond = illustration_instruments()
    raw = illustration_bars()
    prepared = {
        equity.ticker: prepare_series(
            ticker=equity.ticker,
            distribution=equity.distribution,
            trading_currency="GBP",
            bars=raw[equity.ticker],
            calendar=xlon(),
        ),
        bond.ticker: prepare_series(
            ticker=bond.ticker,
            distribution=bond.distribution,
            trading_currency="GBP",
            bars=raw[bond.ticker],
            calendar=xlon(),
        ),
    }
    view = build_view(prepared, date(2024, 1, 31))
    assert view.closes["SYNTH-EQ"][-1] == (date(2024, 1, 31), Decimal("130"))
    assert all(session <= date(2024, 1, 31) for session, _price in view.closes["SYNTH-EQ"])
    honest = TrendFilter(lookback_months=3).target_weights(view, ["SYNTH-EQ", "SYNTH-BOND"])
    assert honest["SYNTH-EQ"] == Decimal("1")
    leaked_points = tuple(
        (session, Decimal("80") if session == date(2024, 1, 31) else price) for session, price in view.closes["SYNTH-EQ"]
    )
    leaked = build_view(
        prepared,
        date(2024, 1, 31),
    )
    del leaked
    from sterling.research.strategy import MarketView

    leaked_view = MarketView(
        as_of=date(2024, 1, 31),
        closes={"SYNTH-EQ": leaked_points, "SYNTH-BOND": view.closes["SYNTH-BOND"]},
    )
    assert TrendFilter(lookback_months=3).target_weights(leaked_view, ["SYNTH-EQ", "SYNTH-BOND"]) == {}
    mutated = []
    for bar in raw["SYNTH-EQ"]:
        if bar.session > date(2024, 1, 31):
            mutated.append(
                Bar(bar.session, Decimal("1000"), Decimal("1000"), Decimal("1000"), Decimal("1000"), bar.volume, quoted_in="GBP")
            )
        else:
            mutated.append(bar)
    original = run_illustration()
    changed = simulate(
        instruments=(equity, bond),
        bars={"SYNTH-EQ": tuple(mutated), "SYNTH-BOND": raw["SYNTH-BOND"]},
        calendar=xlon(),
        strategy=TrendFilter(lookback_months=3),
        limits=load_research_limits(),
        cost_model=illustration_costs(),
    )
    original_january = next(item for item in original.decisions if item["signal_date"] == "2024-01-31")
    changed_january = next(item for item in changed.decisions if item["signal_date"] == "2024-01-31")
    assert original_january["weights"] == changed_january["weights"]
    assert changed_january["data_cutoff"] == "2024-01-31"


def test_deducting_ocf_from_exchange_prices_is_refused() -> None:
    equity, bond = illustration_instruments()
    with pytest.raises(ResearchError, match="OCF"):
        simulate(
            instruments=(equity, bond),
            bars=illustration_bars(),
            calendar=xlon(),
            strategy=TrendFilter(lookback_months=3),
            limits=load_research_limits(),
            deduct_ocf=True,
        )


def test_walk_forward_window_stays_causal() -> None:
    splits = walk_forward_splits(date(2023, 10, 2), date(2024, 5, 31), train_months=4, test_months=3)
    window = splits[-1]
    result = run_illustration(
        evaluate_start=window.test_start,
        evaluate_end=window.test_end,
        label="oos",
    )
    assert result.nav[0].session >= window.test_start
    assert result.nav[-1].session <= window.test_end
    assert all(str(item["data_cutoff"]) <= str(item["signal_date"]) for item in result.decisions)

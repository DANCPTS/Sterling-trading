"""Paper comparison: next-session fills, frozen rules, and a withheld conclusion."""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.calendar import xlon_weekday
from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.compare import (
    FROZEN_CARRY,
    FROZEN_DUAL,
    FROZEN_TREND,
    frozen_carry_weights,
    frozen_trend_weights,
    run_comparison,
    synthetic_bars,
    weekly_decision_days,
)
from sterling.research.grok.preregistration import APPROVED_UNIVERSE
from sterling.research.grok.providers import MockGrokProvider
from sterling.research.strategy import MarketView

ROOT = Path(__file__).resolve().parents[3]


def test_frozen_rules_match_the_registered_defaults_and_ignore_later_prices() -> None:
    assert FROZEN_TREND.lookback_months == 10
    assert FROZEN_TREND.defensive is None
    assert FROZEN_DUAL.lookback_months == 12
    assert FROZEN_DUAL.top_k == 2
    assert FROZEN_DUAL.defensive is None
    assert FROZEN_CARRY.k == 3
    assert FROZEN_CARRY.threshold == Decimal("0.005")
    sessions = xlon_weekday().trading_days(date(2023, 1, 2), date(2024, 6, 28))
    bars = synthetic_bars(sessions, APPROVED_UNIVERSE, seed=3)
    day = weekly_decision_days(sessions)[40]
    history = {
        ticker: tuple((point.session, point.close) for point in series if point.session <= day) for ticker, series in bars.items()
    }
    weights = frozen_trend_weights(day, history, {}, Decimal("10000"))
    future = {ticker: series + ((day + timedelta(days=1), Decimal("1000")),) for ticker, series in history.items()}
    with pytest.raises(ResearchError, match="after"):
        MarketView(as_of=day, closes=future)
    filtered = {ticker: tuple(point for point in series if point[0] <= day) for ticker, series in future.items()}
    assert frozen_trend_weights(day, filtered, {}, Decimal("10000")) == weights
    rates = {"USD": Decimal("0.045"), "EUR": Decimal("0.035"), "JPY": Decimal("0.001"), "GBP": Decimal("0.020")}
    fx_history = {code: ((day - timedelta(days=40), Decimal("1")),) for code in ("USD", "EUR", "JPY")}
    carry = frozen_carry_weights(day, fx_history, rates)
    assert set(carry) == {"EUR", "USD"}
    assert all(weight > 0 for weight in carry.values())
    assert sum(carry.values(), Decimal(0)) == Decimal(1)
    spiked = {code: series + ((day + timedelta(days=1), Decimal("1000")),) for code, series in fx_history.items()}
    assert frozen_carry_weights(day, spiked, rates) == carry


def test_fills_use_the_next_open_and_the_conclusion_is_withheld(tmp_path: Path) -> None:
    sessions = xlon_weekday().trading_days(date(2024, 1, 2), date(2024, 3, 29))
    bars = synthetic_bars(sessions, APPROVED_UNIVERSE, seed=11)
    report = run_comparison(
        bars=bars,
        log_path=tmp_path / "forward.jsonl",
        provider=MockGrokProvider(seed=11),
        calendar=xlon_weekday(),
    )
    assert report.conclusion is False
    assert "conclusion=withheld" in report.text
    assert "technical pilot, not evidence of profitability" in report.text
    assert "execution_permission=NOT_ISSUED" in report.text
    assert "orders_submitted=0" in report.text
    assert report.blocked_grok_decisions >= 0
    cash = next(arm for arm in report.arms if arm.name == "gbp_cash")
    assert cash.end_nav_gbp == Decimal("10000.00")
    assert cash.trades == 0
    held = next(arm for arm in report.arms if arm.name == "buy_and_hold_equal")
    assert held.trades > 0
    assert len({fill.session for fill in report.fills["buy_and_hold_equal"]}) == 1
    grok_fills = report.fills["grok"]
    assert grok_fills
    by_ticker = {ticker: {point.session: point for point in series} for ticker, series in bars.items()}
    for fill in grok_fills:
        assert fill.session > fill.signal_date
        assert fill.mid_price_gbp == by_ticker[fill.ticker][fill.session].open
        assert fill.mid_price_gbp != by_ticker[fill.ticker][fill.signal_date].close
    log = ProposalLog(report.log_path)
    assert log.verify() == report.chain_records
    text = report.log_path.read_text(encoding="utf-8")
    report.log_path.write_text(text.replace("preregistration", "preregistrati0n", 1), encoding="utf-8")
    with pytest.raises(ResearchError, match="chain broke"):
        log.verify()


def test_mock_script_is_offline_and_prints_the_table(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("run_mock_forward_test", ROOT / "scripts" / "run_mock_forward_test.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--output", str(tmp_path)]) == 0
    report = (tmp_path / "report.txt").read_text(encoding="utf-8")
    assert "conclusion=withheld" in report
    assert "technical pilot, not evidence of profitability" in report
    assert "NOT_ISSUED" in report
    assert "grok " in report
    assert "etf_trend" in report
    assert "etf_dual_momentum" in report
    assert "fx_carry" in report
    assert "buy_and_hold_equal" in report
    assert "gbp_cash" in report
    assert "2.50" in report
    assert "5.00" in report
    log = ProposalLog(tmp_path / "forward.jsonl")
    assert log.verify() > 2
    kinds = {row.get("record_type") for row in log.records()}
    assert {"log_created", "preregistration", "snapshot", "proposal", "screen"} <= kinds
    assert "minimum=104/24/30" in report
    created = datetime.fromisoformat(str(log.records()[0]["timestamp_utc"]))
    assert created.tzinfo is not None
    assert created.utcoffset() == timedelta(0)

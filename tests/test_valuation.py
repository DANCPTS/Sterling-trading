"""Accounting valuation labels are not execution quotes."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sterling.ledger.ledger import Ledger, LedgerView
from sterling.ledger.metrics import DailyLossMetrics
from sterling.placeholders.risk import RiskEngine
from tests.support import june, mark, settle

_FORBIDDEN = (
    "all_open_positions_fresh",
    "execution_eligible",
    "trade_permitted",
    "may_trade",
    "trading_allowed",
    "approve_order",
    "quote_is_fresh",
)


def test_unmarked_position_is_cost_basis_for_accounting_only() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    view = ledger.view(as_of=june(1, 10))
    assert view.positions[0].market_value_gbp == Decimal("20.00")
    assert view.positions[0].valuation_status_for_accounting == "ACCOUNTING_COST_BASIS"
    assert view.valuations[0].valuation_status_for_accounting == "ACCOUNTING_COST_BASIS"
    _assert_no_trade_permission(ledger, view, ledger.daily_metrics(june(1, 10), _rules()))


def test_hours_old_same_day_mark_is_not_an_execution_quote() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 9),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    mark(
        ledger,
        event_id="mark-1",
        timestamp=june(1, 9, 5),
        instrument_id="SYNTH-EQ",
        price="12.00",
        source="caller-supplied",
    )
    afternoon = ledger.daily_metrics(june(1, 16), _rules())
    status = afternoon.valuations[0].valuation_status_for_accounting
    assert status == "ACCOUNTING_SAME_DAY"
    assert "FRESH" not in status
    assert afternoon.marked_equity_gbp == Decimal("504.00")
    assert afternoon.valuations[0].mark_source == "caller-supplied"
    _assert_no_trade_permission(ledger, ledger.view(as_of=june(1, 16)), afternoon)

    other_day = ledger.daily_metrics(june(2, 9), _rules())
    assert other_day.valuations[0].valuation_status_for_accounting == "ACCOUNTING_OTHER_DAY"
    assert ledger.view().positions[0].valuation_status_for_accounting == "ACCOUNTING_OTHER_DAY"


def test_mark_after_as_of_is_cost_basis() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="10.00",
    )
    mark(
        ledger,
        event_id="mark-later",
        timestamp=june(1, 16),
        instrument_id="SYNTH-EQ",
        price="11.00",
        source="caller-supplied",
    )
    earlier = ledger.view(as_of=june(1, 12))
    assert earlier.positions[0].valuation_status_for_accounting == "ACCOUNTING_COST_BASIS"
    assert earlier.positions[0].mark_price is None


def test_stage2_quote_checks_are_documented_and_not_implemented() -> None:
    from sterling.placeholders import risk as risk_module

    combined = " ".join(f"{risk_module.__doc__}\n{RiskEngine.__doc__}".split())
    assert "latched daily-loss halt" in combined
    assert "even if equity recovers" in combined
    assert "survives restarts" in combined
    assert "explicit human authorisation" in combined
    assert "no automatic reset on recovery" in combined
    assert "maximum quote age" in combined
    assert "future timestamps" in combined
    assert "fail closed" in combined
    assert "not an execution quote" in combined or "not execution-eligible" in combined
    root = Path(__file__).resolve().parents[1]
    for name in ("docs/rules.md", "docs/design.md"):
        document = " ".join((root / name).read_text(encoding="utf-8").split())
        assert "maximum quote age" in document
        assert "future timestamps" in document
        assert "fail closed" in document
        assert "ACCOUNTING_SAME_DAY" in document


def _assert_no_trade_permission(ledger: Ledger, view: LedgerView, metrics: DailyLossMetrics) -> None:
    for name in _FORBIDDEN:
        assert not hasattr(ledger, name)
        assert not hasattr(view, name)
        assert not hasattr(metrics, name)
        assert not hasattr(RiskEngine, name)
    assert view.positions[0].valuation_status_for_accounting != "FRESH"


def _rules():
    from sterling.rules.loader import load_default_rules

    return load_default_rules()

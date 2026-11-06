"""Unmarked and old marks are labelled, and that label is not permission to trade."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sterling.ledger.ledger import Ledger
from sterling.placeholders.risk import RiskEngine
from tests.support import june, mark, settle


def test_unmarked_position_is_cost_basis_not_fresh() -> None:
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
    position = view.positions[0]
    assert position.market_value_gbp == Decimal("20.00")
    assert position.valuation_status == "COST_BASIS"
    assert position.mark_timestamp is None
    assert position.mark_source is None
    assert view.valuations[0].status == "COST_BASIS"
    assert view.all_open_positions_fresh is False
    metrics = ledger.daily_metrics(june(1, 10), _rules())
    assert metrics.valuations[0].status == "COST_BASIS"
    assert metrics.all_open_positions_fresh is False


def test_same_day_mark_is_fresh_and_a_previous_day_mark_is_stale() -> None:
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
    mark(
        ledger,
        event_id="mark-1",
        timestamp=june(1, 11),
        instrument_id="SYNTH-EQ",
        price="12.00",
        source="caller-supplied",
    )
    fresh = ledger.daily_metrics(june(1, 15), _rules())
    assert fresh.valuations[0].status == "FRESH"
    assert fresh.valuations[0].mark_source == "caller-supplied"
    assert fresh.valuations[0].mark_timestamp == june(1, 11)
    assert fresh.all_open_positions_fresh is True
    assert fresh.marked_equity_gbp == Decimal("504.00")

    stale = ledger.daily_metrics(june(2, 9), _rules())
    assert stale.valuations[0].status == "STALE"
    assert stale.all_open_positions_fresh is False
    assert stale.marked_equity_gbp == Decimal("504.00")

    without_as_of = ledger.view()
    assert without_as_of.positions[0].valuation_status == "STALE"
    assert without_as_of.all_open_positions_fresh is False


def test_mark_after_as_of_is_not_fresh() -> None:
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
    assert earlier.positions[0].valuation_status == "COST_BASIS"
    assert earlier.positions[0].mark_price is None
    assert earlier.all_open_positions_fresh is False


def test_stage2_must_fail_closed_and_latch_the_daily_halt() -> None:
    text = RiskEngine.__doc__ or ""
    module_text = RiskEngine.__module__
    from sterling.placeholders import risk as risk_module

    combined = " ".join(f"{risk_module.__doc__}\n{text}".split())
    assert "latched daily-loss halt" in combined
    assert "even if equity recovers" in combined
    assert "survives restarts" in combined
    assert "explicit human authorisation" in combined
    assert "no automatic reset on recovery" in combined
    assert "fail closed" in combined
    assert "not FRESH" in combined
    assert module_text == "sterling.placeholders.risk"
    root = Path(__file__).resolve().parents[1]
    for name in ("docs/rules.md", "docs/design.md"):
        document = " ".join((root / name).read_text(encoding="utf-8").split())
        assert "even if equity recovers" in document
        assert "explicit human authorisation" in document
        assert "fail closed" in document
        assert "not `FRESH`" in document or "not FRESH" in document


def _rules():
    from sterling.rules.loader import load_default_rules

    return load_default_rules()

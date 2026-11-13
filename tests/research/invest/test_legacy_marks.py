"""Stored legacy marks stay in the log. The page also shows the daily-close reading."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.invest.book import Book, _probe_nav, apply_targets, trade_toward, weights_of
from sterling.research.invest.dashboard import render_dashboard
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.registration import STARTING_CASH_GBP
from sterling.research.invest.schema import CASH, FUNDS, allocation_json
from sterling.research.invest.verify import ledger_warnings, verify_invest

NAV = Decimal("9955.14")
WATER = Decimal("10007.95")
STORED_DRAWDOWN = (WATER - NAV) / WATER
VWRP_END = Decimal("9990.96")
CASH_END = Decimal("10000.95")


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")


def test_sizing_and_a_failed_probe_do_not_keep_the_provisional_high_water(monkeypatch: pytest.MonkeyPatch) -> None:
    book = Book()
    book.cash = WATER
    prices = {name: Decimal("100") for name in FUNDS}
    session = date(2026, 11, 12)
    assert weights_of(book, prices, session) is not None
    trade_toward(book, "VWRP", Decimal("0.20"), Decimal("100"), prices, session)
    targets = {name: Decimal("0.10") for name in FUNDS}
    targets[CASH] = Decimal("0.40")
    apply_targets(book, targets, prices, set(FUNDS), session)
    assert book.peak == STARTING_CASH_GBP
    assert book.max_drawdown == 0
    assert book.nav_rows == []

    fresh = Book()

    def boom(target: Book, _prices: dict[str, Decimal], day: date) -> tuple[Decimal, dict[str, str]]:
        target.peak = WATER
        target.max_drawdown = Decimal("0.9")
        target.nav_rows.append((day, WATER))
        raise RuntimeError("provisional")

    monkeypatch.setattr("sterling.research.invest.book.mark", boom)
    with pytest.raises(RuntimeError, match="provisional"):
        _probe_nav(fresh, {}, session)
    assert fresh.peak == STARTING_CASH_GBP
    assert fresh.max_drawdown == 0
    assert fresh.nav_rows == []


def test_committed_marks_keep_the_daily_close_path() -> None:
    book = Book()
    assert mark_cash(book, Decimal("10000.00"), date(2026, 11, 12)) == STARTING_CASH_GBP
    mark_cash(book, Decimal("9900.00"), date(2026, 11, 13))
    assert book.peak == STARTING_CASH_GBP
    assert book.max_drawdown == Decimal("100") / Decimal("10000")
    book.cash = Decimal("15000.00")
    _probe_nav(book, {}, date(2026, 11, 14))
    assert book.peak == STARTING_CASH_GBP
    assert book.max_drawdown == Decimal("0.01")
    assert [row[1] for row in book.nav_rows] == [Decimal("10000.00"), Decimal("9900.00")]
    mark_cash(book, Decimal("10200.00"), date(2026, 11, 14))
    assert book.peak == Decimal("10200.00")
    assert book.max_drawdown == Decimal("0.01")


def test_decision_snapshot_does_not_commit_a_high_water(tmp_path: Path, stamped: None) -> None:
    del stamped
    book = Book()
    book.cash = WATER
    single = {name: "0.10" for name in FUNDS}
    single[CASH] = "0.40"
    run_invest_week(
        log_path=tmp_path / "week.jsonl",
        book=book,
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=datetime(2026, 11, 2, 16, 30, tzinfo=UTC),
        provider=MockGrokProvider(steps=(ScriptStep(raw_response=allocation_json(single)),), response_format="allocation"),
        prompt="paper-v2",
        mode="simulated_clock",
    )
    assert book.peak == STARTING_CASH_GBP
    assert book.max_drawdown == 0
    assert book.nav_rows == []


def test_old_mock_page_shows_stored_and_daily_close_figures() -> None:
    records = _legacy_records()
    before = [dict(record) for record in records]
    verify_invest(records, lane="mock")
    notes = ledger_warnings(records)
    html = render_dashboard(records)
    assert records == tuple(before)
    assert any("0.4486%" in note and "0.53%" in note and "not an audited intraday drawdown" in note for note in notes)
    assert any("0.0904%" in note and "-0.09%" in note for note in notes)
    assert any("0.0095%" in note and "+0.01%" in note for note in notes)
    assert "0.45% daily close; stored 0.53%" in html
    assert "£10,007.95" in html
    assert "stored 0.00%; from £10,000.00 -0.09%" in html
    assert "stored 0.00%; daily close 0.09%" in html
    assert "stored 0.00%; from £10,000.00 +0.01%" in html
    assert "MOCK / PAPER" in html


def test_benchmark_edges_are_labelled_and_do_not_replace_a_reconciled_row() -> None:
    empty = render_dashboard(())
    assert "No benchmark recorded." in empty
    assert "daily close" not in empty

    stale = render_dashboard(_stale_records())
    assert "Benchmark row 2026-11-12 is older than the latest valuation 2026-11-13" in stale
    assert "stored 0.00%" not in stale

    invalid = render_dashboard(_invalid_arm())
    assert "buy_and_hold_vwrp has no readable end value" in invalid

    multi = render_dashboard(_multi_session())
    assert "stored 0.00%; from £10,000.00 +2.00%" in multi
    assert "stored 0.00%; daily close 1.00%" in multi
    assert "1.0000%" in multi


def mark_cash(book: Book, cash: Decimal, session: date) -> Decimal:
    from sterling.research.invest.book import mark

    book.cash = cash
    marked = mark(book, {}, session)
    assert marked is not None
    return marked[0]


def _legacy_records() -> tuple[dict[str, object], ...]:
    return (
        _identity(
            "decision",
            gate="accepted",
            cash_gbp="10000.00",
            timestamp_utc="2026-11-11T18:00:00+00:00",
            derived_actions={},
        ),
        _identity(
            "valuation",
            session="2026-11-12",
            timestamp_utc="2026-11-12T16:30:00+00:00",
            nav_gbp=format(NAV, "f"),
            cash_gbp=format(NAV, "f"),
            daily_pnl_gbp=format(NAV - Decimal("10000.00"), "f"),
            cumulative_return=format(NAV / Decimal("10000.00") - 1, "f"),
            drawdown=format(STORED_DRAWDOWN, "f"),
            high_water_gbp=format(WATER, "f"),
            holdings={},
            prices={},
            data_label="live",
        ),
        _identity(
            "performance",
            session="2026-11-12",
            data_label="live",
            arms=[_legacy_arm("buy_and_hold_vwrp", VWRP_END), _legacy_arm("sterling_cash", CASH_END)],
        ),
    )


def _stale_records() -> tuple[dict[str, object], ...]:
    end = Decimal("10100.00")
    total = end / Decimal("10000.00") - 1
    return (
        _identity("decision", gate="accepted", cash_gbp="10000.00", derived_actions={}),
        _identity(
            "valuation",
            session="2026-11-13",
            nav_gbp="10100.00",
            cash_gbp="10100.00",
            daily_pnl_gbp="100.00",
            cumulative_return=format(total, "f"),
            drawdown="0",
            high_water_gbp="10100.00",
            holdings={},
            prices={},
            data_label="live",
        ),
        _identity(
            "performance",
            session="2026-11-12",
            arms=[
                {
                    "name": "sterling_cash",
                    "end_nav_gbp": format(end, "f"),
                    "start_nav_gbp": "10000.00",
                    "cumulative_return": format(total, "f"),
                    "max_drawdown": "0",
                    "trades": 0,
                    "costs_gbp": "0.00",
                }
            ],
        ),
    )


def _invalid_arm() -> tuple[dict[str, object], ...]:
    return (
        _identity("decision", gate="accepted", cash_gbp="10000.00", derived_actions={}),
        _identity(
            "valuation",
            session="2026-11-12",
            nav_gbp="10000.00",
            cash_gbp="10000.00",
            daily_pnl_gbp="0.00",
            cumulative_return="0",
            drawdown="0",
            high_water_gbp="10000.00",
            holdings={},
            prices={},
            data_label="live",
        ),
        _identity(
            "performance",
            session="2026-11-12",
            arms=[{"name": "buy_and_hold_vwrp", "end_nav_gbp": "n/a", "cumulative_return": "0", "max_drawdown": "0"}],
        ),
    )


def _multi_session() -> tuple[dict[str, object], ...]:
    return (
        _identity("decision", gate="accepted", cash_gbp="10000.00", derived_actions={}),
        _identity(
            "valuation",
            session="2026-11-12",
            nav_gbp="9900.00",
            cash_gbp="9900.00",
            daily_pnl_gbp="-100.00",
            cumulative_return="-0.01",
            drawdown="0.01",
            high_water_gbp="10000.00",
            holdings={},
            prices={},
            data_label="live",
        ),
        _identity(
            "valuation",
            session="2026-11-13",
            nav_gbp="10200.00",
            cash_gbp="10200.00",
            daily_pnl_gbp="200.00",
            cumulative_return="0.02",
            drawdown="0.01",
            high_water_gbp="10200.00",
            holdings={},
            prices={},
            data_label="live",
        ),
        _identity("performance", session="2026-11-12", arms=[_legacy_arm("buy_and_hold_vwrp", Decimal("9900.00"))]),
        _identity("performance", session="2026-11-13", arms=[_legacy_arm("buy_and_hold_vwrp", Decimal("10200.00"))]),
    )


def _legacy_arm(name: str, end: Decimal) -> dict[str, object]:
    text = format(end, "f")
    return {
        "name": name,
        "end_nav_gbp": text,
        "start_nav_gbp": text,
        "cumulative_return": "0",
        "max_drawdown": "0",
        "trades": 0,
        "costs_gbp": "0.00",
    }


def _identity(kind: str, **fields: object) -> dict[str, object]:
    return {
        "system": "sterling-invest",
        "experiment": "paper-v2",
        "evidence_lane": "mock",
        "record_type": kind,
        **fields,
    }

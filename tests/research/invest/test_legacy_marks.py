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
    assert any("0.4486%" in note and "0.53%" in note and "reconstructed from older evidence" in note.lower() for note in notes)
    assert any("-0.0904%" in note and "reconstructed from older evidence" in note.lower() for note in notes)
    assert any("+0.0095%" in note and "reconstructed from older evidence" in note.lower() for note in notes)
    assert "Daily-close drawdown" in html
    assert "0.4486%" in html
    assert "0.45% reconstructed from older evidence" in html
    assert "£10,007.95" in html
    assert "reconstructed from older evidence -0.0904% (stored 0.00%)" in html
    assert "reconstructed from older evidence +0.0095% (stored 0.00%)" in html
    assert "reconstructed from older evidence 0.0904% (stored 0.00%)" in html
    assert "MOCK / PAPER" in html
    assert "£9,955.14" in html
    assert "£1,428.41" in html
    assert "£8.57" in html
    assert "£0.95" in html
    assert html.count(">BUY<") == 6
    assert "2026-11-12, not real-time" in html
    assert "fresh" not in html
    assert "Awaiting session" in html
    assert "DATA LABEL: live" not in html


def test_mixed_legacy_and_corrected_sessions_do_not_look_like_a_falling_peak() -> None:
    day_one = (Decimal("10007.95") - Decimal("9955.14")) / Decimal("10007.95")
    records = (
        _identity("decision", gate="accepted", cash_gbp="10000.00", derived_actions={}),
        _close("2026-11-12", "9955.14", "-44.86", format(day_one, "f"), "10007.95"),
        _close("2026-11-13", "9947.58", "-7.56", "0.005242", "10000.00"),
    )
    notes = ledger_warnings(records)
    html = render_dashboard(records)
    text = " ".join(notes)
    assert "High water on 2026-11-13 fell." not in text
    assert "fell below the earlier maximum" not in text
    assert text.lower().count("legacy accounting discrepancy") == 1
    assert "provisional sizing mark" in text
    assert "0.4486%" in text
    assert "0.5242%" in text
    assert "not an error just because it differs from this legacy peak" in text
    assert "0.52%" in html
    assert "£9,947.58" in html
    fresh = (
        _identity("decision", gate="accepted", cash_gbp="10000.00", derived_actions={}),
        _close("2026-11-12", "9955.14", "-44.86", "0.004486", "10000.00"),
        _close("2026-11-13", "9947.58", "-7.56", "0.005242", "10000.00"),
    )
    fresh_notes = " ".join(ledger_warnings(fresh))
    assert "fell" not in fresh_notes
    assert "Legacy accounting discrepancy" not in fresh_notes
    broken = (
        _identity("decision", gate="accepted", cash_gbp="10000.00", derived_actions={}),
        _close("2026-11-12", "11000.00", "1000.00", "0", "11000.00"),
        _close("2026-11-13", "10500.00", "-500.00", "0", "10000.00"),
    )
    broken_notes = " ".join(ledger_warnings(broken))
    assert "Stored high water on 2026-11-13 is £10,000.00" in broken_notes
    assert "disagrees with that reconstruction" in broken_notes
    assert "Legacy accounting discrepancy" not in broken_notes


def test_a_later_inflated_peak_is_not_called_legacy() -> None:
    day_one = (Decimal("10007.95") - Decimal("9955.14")) / Decimal("10007.95")
    inflated = (Decimal("15000.00") - Decimal("9900.00")) / Decimal("15000.00")
    records = (
        _identity("decision", gate="accepted", cash_gbp="10000.00", derived_actions={}),
        _close("2026-11-12", "9955.14", "-44.86", format(day_one, "f"), "10007.95"),
        _close("2026-11-13", "9947.58", "-7.56", "0.005242", "10000.00"),
        _close("2026-11-14", "9900.00", "-47.58", format(inflated, "f"), "15000.00"),
    )
    text = " ".join(ledger_warnings(records))
    assert "Legacy accounting discrepancy on 2026-11-12" in text
    assert "Stored high water on 2026-11-14 is £15,000.00" in text
    assert "disagrees with that reconstruction" in text
    assert "Stored max drawdown on 2026-11-14" in text
    legacy, _, later = text.partition("Stored high water on 2026-11-14")
    assert "£15,000.00" not in legacy
    too_low = _close("2026-11-16", "10080.00", "180.00", "0.001", "10100.00")
    too_high = _close("2026-11-16", "10080.00", "180.00", "0.20", "10100.00")
    base = (
        _identity("decision", gate="accepted", cash_gbp="10000.00", derived_actions={}),
        _close("2026-11-12", "9955.14", "-44.86", "0.004486", "10000.00"),
        _close("2026-11-13", "9947.58", "-7.56", "0.005242", "10000.00"),
        _close("2026-11-14", "10100.00", "152.42", "0.005242", "10100.00"),
    )
    low_notes = " ".join(ledger_warnings((*base, too_low)))
    high_notes = " ".join(ledger_warnings((*base, too_high)))
    assert "Stored max drawdown on 2026-11-16" in low_notes
    assert "Stored max drawdown on 2026-11-16" in high_notes
    assert "Legacy accounting discrepancy" not in low_notes
    assert "Legacy accounting discrepancy" not in high_notes
    assert "cumulative maximum, not the single-day gap" in low_notes
    assert "cumulative maximum, not the single-day gap" in high_notes


def test_second_session_fixture_shows_the_published_totals() -> None:
    """Economics published for 13 Nov. This is not the private 22-row log."""
    records = _second_session()
    html = render_dashboard(records)
    notes = " ".join(ledger_warnings(records))
    assert "£10,000.00" in html
    assert "£9,947.58" in html
    assert "-£7.56" in html
    assert "-£52.42" in html
    assert "0.52%" in html
    assert "0.5242%" in notes
    assert "0.4486%" in notes
    assert "£1,428.55" in html
    assert "£8.57" in html
    assert "£1.09" in html
    assert "-£53.51" in html
    assert html.count(">BUY<") == 6
    assert ">SELL<" not in html
    assert notes.lower().count("legacy accounting discrepancy") == 1
    assert "provisional sizing mark" in notes
    assert "High water on 2026-11-13 fell." not in notes
    assert "fell below the earlier maximum" not in notes
    assert "reconstructed from older evidence -0.0904%" in html
    assert "reconstructed from older evidence +0.0095%" in html
    assert "older than the latest valuation 2026-11-13" in notes


def test_later_sessions_follow_the_reconstruction_not_the_legacy_peak() -> None:
    good = _path_after_the_legacy_mark(correct=True)
    notes = " ".join(ledger_warnings(good))
    assert notes.lower().count("legacy accounting discrepancy") == 1
    assert "disagrees with that reconstruction" not in notes
    assert "fell below the earlier maximum" not in notes
    wrong_peak = _path_after_the_legacy_mark(correct=True)
    valuations = [record for record in wrong_peak if record["record_type"] == "valuation"]
    valuations[-2]["high_water_gbp"] = "10000.00"
    flagged = " ".join(ledger_warnings(wrong_peak))
    assert "Stored high water on 2026-11-17 is £10,000.00" in flagged
    assert "disagrees with that reconstruction" in flagged
    wrong_drawdown = _path_after_the_legacy_mark(correct=True)
    last = [record for record in wrong_drawdown if record["record_type"] == "valuation"][-1]
    last["drawdown"] = format((Decimal("10100.00") - Decimal("10080.00")) / Decimal("10100.00"), "f")
    drawdown_notes = " ".join(ledger_warnings(wrong_drawdown))
    assert "Stored max drawdown on 2026-11-18" in drawdown_notes
    assert "cumulative maximum, not the single-day gap" in drawdown_notes


def test_benchmark_edges_are_labelled_and_do_not_replace_a_reconciled_row() -> None:
    empty = render_dashboard(())
    assert "No benchmark recorded." in empty
    assert "reconstructed from older evidence" not in empty

    stale = render_dashboard(_stale_records())
    assert "Benchmark row 2026-11-12 is older than the latest valuation 2026-11-13" in stale
    assert "stored 0.00%" not in stale

    invalid = render_dashboard(_invalid_arm())
    assert "buy_and_hold_vwrp has no readable end value" in invalid

    multi = render_dashboard(_multi_session())
    assert "reconstructed from older evidence +2.00% (stored 0.00%)" in multi
    assert "reconstructed from older evidence 1.00% (stored 0.00%)" in multi


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
            "cash_interest",
            session="2026-11-12",
            status="accrued",
            amount_gbp="0.95",
        ),
        *[
            _identity(
                "fill",
                session="2026-11-12",
                fund=name,
                side="BUY",
                units="1",
                price_gbp="100.00",
                cash_delta_gbp="-100.00",
            )
            for name in FUNDS
        ],
        _identity(
            "valuation",
            session="2026-11-12",
            timestamp_utc="2026-11-12T16:30:00+00:00",
            nav_gbp=format(NAV, "f"),
            cash_gbp="1428.41",
            daily_pnl_gbp=format(NAV - Decimal("10000.00"), "f"),
            cumulative_return=format(NAV / Decimal("10000.00") - 1, "f"),
            cumulative_costs_gbp="8.57",
            drawdown=format(STORED_DRAWDOWN, "f"),
            high_water_gbp=format(WATER, "f"),
            holdings={name: "1" for name in FUNDS},
            prices={
                name: {"price_gbp": price, "status": "fresh", "last_good": "2026-11-12"}
                for name, price in zip(FUNDS, ("1421.13", "1421.12", "1421.12", "1421.12", "1421.12", "1421.12"), strict=True)
            },
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


def _second_session() -> tuple[dict[str, object], ...]:
    prices = ("1419.83", "1419.83", "1419.83", "1419.83", "1419.83", "1419.88")
    spends = ("-1428.75", "-1428.75", "-1428.75", "-1428.75", "-1428.75", "-1428.79")
    fills = [
        _identity(
            "fill",
            session="2026-11-12",
            fund=name,
            side="BUY",
            units="1",
            price_gbp="100.00",
            cash_delta_gbp=spend,
        )
        for name, spend in zip(FUNDS, spends, strict=True)
    ]
    return (
        _identity("decision", gate="accepted", cash_gbp="10000.00", timestamp_utc="2026-11-11T18:00:00+00:00", derived_actions={}),
        *fills,
        _identity("cash_interest", session="2026-11-12", status="accrued", amount_gbp="0.95"),
        _identity("cash_interest", session="2026-11-13", status="accrued", amount_gbp="0.14"),
        _close("2026-11-12", "9955.14", "-44.86", format(STORED_DRAWDOWN, "f"), "10007.95"),
        _close("2026-11-13", "9947.58", "-7.56", "0.005242", "10000.00", cash="1428.55", costs="8.57", holdings=prices),
        _identity(
            "performance",
            session="2026-11-12",
            arms=[
                _legacy_arm("buy_and_hold_vwrp", Decimal("9990.96")),
                _legacy_arm("equal_weight", Decimal("9990.96")),
                _legacy_arm("sixty_forty", Decimal("9990.96")),
                _legacy_arm("sterling_cash", Decimal("10000.95")),
            ],
        ),
    )


def _path_after_the_legacy_mark(*, correct: bool) -> tuple[dict[str, object], ...]:
    del correct
    stored = (Decimal("10007.95") - Decimal("9955.14")) / Decimal("10007.95")
    steps = (
        ("2026-11-12", "9955.14", "-44.86", format(stored, "f"), "10007.95"),
        ("2026-11-13", "9947.58", "-7.56", "0.005242", "10000.00"),
        ("2026-11-16", "10050.00", "102.42", "0.005242", "10050.00"),
        ("2026-11-17", "10100.00", "50.00", "0.005242", "10100.00"),
        ("2026-11-18", "10080.00", "-20.00", "0.005242", "10100.00"),
    )
    return (
        _identity("decision", gate="accepted", cash_gbp="10000.00", derived_actions={}),
        *(_close(*step) for step in steps),
    )


def _close(
    session: str,
    nav: str,
    daily: str,
    drawdown: str,
    water: str,
    cash: str | None = None,
    costs: str = "0.00",
    holdings: tuple[str, ...] | None = None,
) -> dict[str, object]:
    marked = {name: "1" for name in FUNDS} if holdings else {}
    prices = (
        {name: {"price_gbp": price, "last_good": session, "status": "fresh"} for name, price in zip(FUNDS, holdings, strict=True)}
        if holdings
        else {}
    )
    return _identity(
        "valuation",
        session=session,
        nav_gbp=nav,
        cash_gbp=cash or nav,
        daily_pnl_gbp=daily,
        cumulative_costs_gbp=costs,
        cumulative_return=format(Decimal(nav) / Decimal("10000.00") - 1, "f"),
        drawdown=drawdown,
        high_water_gbp=water,
        holdings=marked,
        prices=prices,
        data_label="live",
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

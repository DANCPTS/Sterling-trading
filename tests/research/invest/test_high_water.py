"""Provisional marks must not become the recorded high water."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.invest.book import Book, _probe_nav, apply_targets, mark, trade_toward, weights_of
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.registration import STARTING_CASH_GBP
from sterling.research.invest.schema import CASH, FUNDS, allocation_json

WATER = Decimal("10007.95")


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
    assert _mark_cash(book, Decimal("10000.00"), date(2026, 11, 12)) == STARTING_CASH_GBP
    _mark_cash(book, Decimal("9900.00"), date(2026, 11, 13))
    assert book.peak == STARTING_CASH_GBP
    first_loss = Decimal("100") / Decimal("10000")
    assert book.max_drawdown == first_loss
    book.cash = Decimal("15000.00")
    _probe_nav(book, {}, date(2026, 11, 14))
    assert book.peak == STARTING_CASH_GBP
    assert book.max_drawdown == first_loss
    assert [row[1] for row in book.nav_rows] == [Decimal("10000.00"), Decimal("9900.00")]
    _mark_cash(book, Decimal("10200.00"), date(2026, 11, 14))
    assert book.peak == Decimal("10200.00")
    assert book.max_drawdown == first_loss


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


def _mark_cash(book: Book, cash: Decimal, session: date) -> Decimal:
    book.cash = cash
    marked = mark(book, {}, session)
    assert marked is not None
    return marked[0]

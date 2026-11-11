"""Fills wait for the first XLON open strictly after the decision."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.grok.quotes import gbp_divisor, to_gbp
from sterling.research.invest.book import Book
from sterling.research.invest.cash_rate import cash_quote, parse_sonia_csv
from sterling.research.invest.daily import run_daily
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.market import select_market
from sterling.research.invest.schema import CASH, FUNDS, allocation_json
from sterling.research.invest.sessions import first_session_after, session_open

LONDON = ZoneInfo("Europe/London")
SONIA = ((date(2026, 10, 30), Decimal("4.00")),)


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")


def test_open_rules_cover_weekend_holiday_and_dst() -> None:
    evening = datetime(2026, 11, 11, 21, 58, tzinfo=UTC)
    assert first_session_after(evening) == date(2026, 11, 12)
    before_open = datetime(2026, 11, 11, 7, 0, tzinfo=LONDON)
    assert first_session_after(before_open) == date(2026, 11, 11)
    assert session_open(date(2026, 11, 11)) == datetime(2026, 11, 11, 8, 0, tzinfo=LONDON)
    friday = datetime(2026, 11, 13, 18, 0, tzinfo=LONDON)
    assert first_session_after(friday) == date(2026, 11, 16)
    bank_holiday_eve = datetime(2026, 5, 1, 18, 0, tzinfo=LONDON)
    assert session_open(date(2026, 5, 4)) is None
    assert first_session_after(bank_holiday_eve) == date(2026, 5, 5)
    early_bst = datetime(2026, 3, 30, 7, 30, tzinfo=LONDON)
    late_bst = datetime(2026, 3, 30, 8, 30, tzinfo=LONDON)
    assert first_session_after(early_bst) == date(2026, 3, 30)
    assert first_session_after(late_bst) == date(2026, 3, 31)
    assert session_open(date(2026, 3, 30)).utcoffset() == timedelta(hours=1)
    assert session_open(date(2026, 10, 26)).utcoffset() == timedelta(0)


def test_same_day_and_earlier_sessions_do_not_fill(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "lane.jsonl"
    decision = datetime(2026, 11, 11, 21, 58, tzinfo=UTC)
    _decide(log, decision)
    blocked = _day(log, date(2026, 11, 11), decision + timedelta(hours=1))
    assert blocked["status"] == "marked"
    text = log.read_text(encoding="utf-8")
    assert '"record_type":"fill"' not in text
    assert "open_not_after_decision" in text
    _day(log, date(2026, 11, 10), decision)
    assert '"session":"2026-11-10"' in log.read_text(encoding="utf-8")
    filled = _day(log, date(2026, 11, 12), datetime(2026, 11, 12, 17, tzinfo=UTC))
    assert filled["status"] == "marked"
    body = log.read_text(encoding="utf-8")
    assert body.count('"record_type":"fill"') == len(FUNDS)
    rerun = _day(log, date(2026, 11, 12), datetime(2026, 11, 12, 18, tzinfo=UTC))
    assert rerun["status"] == "noop"
    assert log.read_text(encoding="utf-8").count('"record_type":"fill"') == len(FUNDS)
    morning = tmp_path / "morning.jsonl"
    _decide(morning, datetime(2026, 11, 11, 7, 0, tzinfo=LONDON))
    same = _day(morning, date(2026, 11, 11), datetime(2026, 11, 11, 16, 30, tzinfo=LONDON))
    assert same["status"] == "marked"
    assert '"record_type":"fill"' in morning.read_text(encoding="utf-8")


def test_missing_first_open_fills_on_the_next_session(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "lane.jsonl"
    decision = datetime(2026, 11, 11, 18, 0, tzinfo=LONDON)
    _decide(log, decision)
    before = _day(log, date(2026, 11, 11), datetime(2026, 11, 11, 16, 30, tzinfo=LONDON))
    assert before["status"] == "marked"
    assert '"record_type":"fill"' not in log.read_text(encoding="utf-8")
    thursday = _day(log, date(2026, 11, 12), datetime(2026, 11, 12, 16, 30, tzinfo=LONDON), opens={})
    assert thursday["status"] == "marked"
    waiting = log.read_text(encoding="utf-8")
    assert '"record_type":"fill"' not in waiting
    assert waiting.count("opening_price_unavailable") == len(FUNDS)
    friday = _day(log, date(2026, 11, 13), datetime(2026, 11, 13, 16, 30, tzinfo=LONDON))
    assert friday["status"] == "marked"
    filled = log.read_text(encoding="utf-8")
    assert filled.count('"record_type":"fill"') == len(FUNDS)
    for line in filled.splitlines():
        if '"record_type":"fill"' in line:
            assert '"session":"2026-11-13"' in line
    retry = _day(log, date(2026, 11, 13), datetime(2026, 11, 13, 18, 0, tzinfo=LONDON))
    assert retry["status"] == "noop"
    assert log.read_text(encoding="utf-8").count('"record_type":"fill"') == len(FUNDS)


def test_sonia_csv_and_publication_lag() -> None:
    text = "DATE,IUDSOIA\n09 Nov 2026,3.9684\n10 Nov 2026,3.9701\n"
    parsed = parse_sonia_csv(text)
    assert parsed[0] == (date(2026, 11, 9), Decimal("3.9684"))
    valuation = datetime(2026, 11, 11, 16, 30, tzinfo=LONDON)
    quote = cash_quote(parsed, date(2026, 11, 11), as_of=valuation)
    assert quote is not None
    assert quote.published_on == date(2026, 11, 10)
    assert quote.available_at == datetime(2026, 11, 11, 9, 0, tzinfo=LONDON)
    assert cash_quote(((date(2026, 11, 11), Decimal("4")),), date(2026, 11, 11), as_of=valuation) is None
    pence = to_gbp(Decimal("18131"), "GBp", gbp_divisor("XSTR"))
    assert pence == Decimal("181.31")


def test_fixture_market_drops_future_prints_and_names_the_cross_check() -> None:
    as_of = datetime(2026, 11, 12, 17, 0, tzinfo=UTC)
    session = date(2026, 11, 12)
    opened = session_open(session)
    assert opened is not None
    closes = [
        ("VWRP", session, Decimal("101"), datetime(2026, 11, 12, 16, 30, tzinfo=UTC)),
        ("VWRP", date(2026, 11, 13), Decimal("999"), datetime(2026, 11, 13, 16, 30, tzinfo=UTC)),
    ]
    opens = [("VWRP", session, Decimal("100"), opened)]
    second = [("VWRP", session, "ishares-nav")]
    live_opens, history, notes = select_market(
        closes=closes,
        opens=opens,
        second_sources=second,
        session=session,
        as_of=as_of,
    )
    assert live_opens == {"VWRP": Decimal("100")}
    assert history["VWRP"] == ((session, Decimal("101")),)
    assert notes["VWRP"] == "cross-checked against ishares-nav"
    assert notes["CUKX"].startswith("yahoo-chart only")


def _decide(log: Path, when: datetime) -> None:
    weights = {name: format(Decimal(1) / Decimal(7), "f") for name in (*FUNDS, CASH)}
    run_invest_week(
        log_path=log,
        book=Book(),
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=when,
        provider=MockGrokProvider(steps=(ScriptStep(raw_response=allocation_json(weights)),), response_format="allocation"),
        prompt="paper-v2",
        mode="simulated_clock",
    )


def _day(log: Path, session: date, recorded_at: datetime, opens: dict[str, Decimal] | None = None):
    return run_daily(
        log_path=log,
        session=session,
        opens={name: Decimal("101") for name in FUNDS} if opens is None else opens,
        close_history={name: ((session, Decimal("101")),) for name in FUNDS},
        sonia=SONIA,
        recorded_at=recorded_at,
        open_source="SYNTHETIC-open",
        data_label="SYNTHETIC",
    )

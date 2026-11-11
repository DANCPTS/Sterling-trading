"""Daily settlement: one open, one interest line, one valuation, no model."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.invest.book import Book
from sterling.research.invest.cash_rate import HAIRCUT_PERCENTAGE_POINTS, cash_quote, parse_sonia_csv
from sterling.research.invest.daily import load_book, run_daily
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.report import _money, _pct, write_dashboard
from sterling.research.invest.schema import CASH, FUNDS, allocation_json
from sterling.research.money import quantize_money

ROOT = Path(__file__).resolve().parents[3]
WHEN = datetime(2026, 11, 2, 16, 30, tzinfo=UTC)
SONIA = ((WHEN.date() - timedelta(days=1), Decimal("4.00")), (WHEN.date() + timedelta(days=20), Decimal("9.00")))


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")


def test_dashboard_formats_money_and_percentages() -> None:
    assert _money("10613.0912") == "10,613.09"
    assert _money("19.780") == "19.78"
    assert _pct("0.061309") == "6.13%"
    assert _pct("0.000761") == "0.08%"
    assert "." in _pct("0.000761")
    assert "0.000761" not in _pct("0.000761")


def test_sonia_ignores_a_later_print() -> None:
    quote = cash_quote(SONIA, WHEN.date())
    assert quote is not None
    assert quote.published_on == WHEN.date() - timedelta(days=1)
    assert quote.sonia_percent == Decimal("4.00")
    assert quote.haircut_percentage_points == HAIRCUT_PERCENTAGE_POINTS
    assert quote.annual_decimal == Decimal("0.035")
    text = "DATE,IUDSOIA\n02 Nov 2026,3.97\n"
    assert parse_sonia_csv(text) == ((WHEN.date(), Decimal("3.97")),)


def test_daily_rerun_and_restart_do_not_double(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "mock" / "forward.jsonl"
    _decide(log)
    day = WHEN.date() + timedelta(days=1)
    history = {name: ((day, Decimal("101")),) for name in FUNDS}
    opens = {name: Decimal("101") for name in FUNDS}
    opens.pop("XSTR")
    first = run_daily(
        log_path=log,
        session=day,
        opens=opens,
        close_history=history,
        sonia=SONIA,
        recorded_at=WHEN + timedelta(days=1),
        open_source="SYNTHETIC-open",
        data_label="SYNTHETIC",
        stop_after="fills",
    )
    assert first["provider_calls"] == 0
    text = log.read_text(encoding="utf-8")
    fills = text.count('"record_type":"fill"')
    interest = text.count('"record_type":"cash_interest"')
    assert fills >= 1
    assert interest == 1
    assert "opening_price_unavailable" in text
    assert "XSTR" in text
    finished = run_daily(
        log_path=log,
        session=day,
        opens={**opens, "XSTR": Decimal("101")},
        close_history=history,
        sonia=SONIA,
        recorded_at=WHEN + timedelta(days=1, minutes=5),
        open_source="SYNTHETIC-open",
        data_label="SYNTHETIC",
    )
    assert finished["status"] == "marked"
    again = log.read_text(encoding="utf-8")
    assert again.count('"record_type":"fill"') == fills + 1
    assert again.count('"record_type":"cash_interest"') == 1
    day_marks = [line for line in again.splitlines() if '"record_type":"valuation"' in line and "2026-11-03" in line]
    assert len(day_marks) == 1
    noop = run_daily(
        log_path=log,
        session=day,
        opens={**opens, "XSTR": Decimal("101")},
        close_history=history,
        sonia=SONIA,
        recorded_at=WHEN + timedelta(days=1, minutes=10),
        open_source="SYNTHETIC-open",
        data_label="SYNTHETIC",
    )
    assert noop["status"] == "noop"
    assert log.read_text(encoding="utf-8") == again
    stored = again
    assert "4.00" in stored
    assert "IUDSOIA" in stored
    page = tmp_path / "dashboard.html"
    assert write_dashboard(log, page) == 0
    html = page.read_text(encoding="utf-8")
    assert "sterling-invest" in html
    assert "SYNTHETIC" in html
    page.write_text("stale", encoding="utf-8")
    broken = tmp_path / "broken.jsonl"
    broken.write_text("{}\n", encoding="utf-8")
    assert write_dashboard(broken, page) == 1
    assert page.read_text(encoding="utf-8") == "stale"


def test_missing_first_sonia_day_is_backfilled_once(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "interest.jsonl"
    thursday = date(2026, 11, 12)
    friday = date(2026, 11, 13)
    series = ((thursday, Decimal("4.00")), (date(2026, 11, 20), Decimal("9.00")))
    _mark(log, thursday, datetime(2026, 11, 12, 16, 30, tzinfo=ZoneInfo("Europe/London")), series)
    waiting = log.read_text(encoding="utf-8")
    assert '"record_type":"cash_interest"' not in waiting
    assert waiting.count('"record_type":"interest_pending"') == 1
    assert '"status":"pending"' in waiting
    assert "amount_gbp" not in waiting.split('"record_type":"interest_pending"')[1]
    again = _mark(log, thursday, datetime(2026, 11, 12, 18, 0, tzinfo=ZoneInfo("Europe/London")), series)
    assert again["status"] == "noop"
    assert log.read_text(encoding="utf-8").count('"record_type":"interest_pending"') == 1
    _mark(log, friday, datetime(2026, 11, 13, 16, 30, tzinfo=ZoneInfo("Europe/London")), series)
    amounts = _interest_rows(log)
    assert [day for day, _amount, _rate in amounts] == ["2026-11-12", "2026-11-13"]
    assert _accrued(Decimal("10000.00"), [Decimal("0.035"), Decimal("0.035")]) == sum(amount for _day, amount, _rate in amounts)
    book = load_book(log)
    assert book.cash == Decimal("10000.00") + sum(amount for _day, amount, _rate in amounts)
    assert book.accrued_through == friday
    assert all(rate == Decimal("0.035") for _day, _amount, rate in amounts)
    rerun = _mark(log, friday, datetime(2026, 11, 13, 18, 0, tzinfo=ZoneInfo("Europe/London")), series)
    assert rerun["status"] == "noop"
    assert _interest_rows(log) == amounts


def test_sonia_gap_before_a_later_accrual_is_backfilled_once(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "gap.jsonl"
    london = ZoneInfo("Europe/London")
    early = ((date(2026, 11, 12), Decimal("4.00")), (date(2026, 11, 20), Decimal("9.00")))
    _mark(log, date(2026, 11, 11), datetime(2026, 11, 11, 16, 30, tzinfo=london), ())
    held = log.read_text(encoding="utf-8")
    assert '"record_type":"cash_interest"' not in held
    assert '"record_type":"interest_pending"' in held
    assert '"status":"pending"' in held
    assert load_book(log).accrued_through is None
    _mark(log, date(2026, 11, 13), datetime(2026, 11, 13, 16, 30, tzinfo=london), early)
    first = _interest_rows(log)
    assert [day for day, _amount, _rate in first] == ["2026-11-12", "2026-11-13"]
    later = ((date(2026, 11, 11), Decimal("3.00")), *early)
    _mark(log, date(2026, 11, 16), datetime(2026, 11, 16, 16, 30, tzinfo=london), later)
    rows = _interest_rows(log)
    assert [day for day, _amount, _rate in rows] == [
        "2026-11-12",
        "2026-11-13",
        "2026-11-11",
        "2026-11-14",
        "2026-11-15",
        "2026-11-16",
    ]
    rates = {day: rate for day, _amount, rate in rows}
    amounts = {day: amount for day, amount, _rate in rows}
    assert rates["2026-11-11"] == Decimal("0.025")
    assert amounts["2026-11-11"] == quantize_money(Decimal("10000.00") * Decimal("0.025") / Decimal(365))
    assert rates["2026-11-14"] == Decimal("0.035")
    assert "9" not in "".join(format(rate, "f") for _day, _amount, rate in rows)
    total = _replay_cash(rows)
    caught_up = load_book(log)
    assert caught_up.cash == total
    assert caught_up.accrued_through == date(2026, 11, 16)
    assert log.read_text(encoding="utf-8").count('"session":"2026-11-11"') >= 1
    pending = [line for line in log.read_text(encoding="utf-8").splitlines() if "interest_pending" in line]
    assert len(pending) == 1 and "amount_gbp" not in pending[0]
    stored = log.read_text(encoding="utf-8")
    rerun = _mark(log, date(2026, 11, 16), datetime(2026, 11, 16, 18, 0, tzinfo=london), later)
    assert rerun["status"] == "noop"
    assert log.read_text(encoding="utf-8") == stored
    assert load_book(log).cash == total


def test_daily_code_does_not_call_a_model() -> None:
    daily = (ROOT / "src/sterling/research/invest/daily.py").read_text(encoding="utf-8")
    script = (ROOT / "scripts/run_invest_daily.py").read_text(encoding="utf-8")
    workflow = (ROOT / "deploy/private-runner/.github/workflows/daily-invest.yml").read_text(encoding="utf-8")
    assert "xai_client" not in daily and "MockGrok" not in daily
    assert "xai_client" not in script
    assert "if: false" in workflow
    assert "cron:" not in workflow
    assert "XAI_API_KEY" not in workflow


def _mark(log: Path, session: date, recorded_at: datetime, sonia: tuple[tuple[date, Decimal], ...]):
    return run_daily(
        log_path=log,
        session=session,
        opens={},
        close_history={},
        sonia=sonia,
        recorded_at=recorded_at,
        open_source="SYNTHETIC-open",
        data_label="SYNTHETIC",
    )


def _interest_rows(log: Path) -> list[tuple[str, Decimal, Decimal]]:
    import json

    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("record_type") != "cash_interest":
            continue
        assert str(record["published_on"]) <= str(record["session"])
        assert record["sonia_percent"] != "9.00"
        rows.append((str(record["session"]), Decimal(str(record["amount_gbp"])), Decimal(str(record["annual_decimal"]))))
    return rows


def _accrued(cash: Decimal, rates: list[Decimal]) -> Decimal:
    total = Decimal("0.00")
    balance = cash
    for rate in rates:
        amount = quantize_money(balance * rate / Decimal(365))
        balance += amount
        total += amount
    return total


def _replay_cash(rows: list[tuple[str, Decimal, Decimal]]) -> Decimal:
    balance = Decimal("10000.00")
    for _day, amount, _rate in rows:
        balance = quantize_money(balance + amount)
    return balance


def _decide(log: Path) -> None:
    single = {name: "0.10" for name in FUNDS}
    single[CASH] = "0.40"
    run_invest_week(
        log_path=log,
        book=Book(),
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=WHEN,
        provider=MockGrokProvider(steps=(ScriptStep(raw_response=allocation_json(single)),), response_format="allocation"),
        prompt="paper-v2",
        mode="simulated_clock",
    )

"""Daily settlement: one open, one interest line, one valuation, no model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.invest.book import Book
from sterling.research.invest.cash_rate import HAIRCUT_PERCENTAGE_POINTS, cash_quote, parse_sonia_csv
from sterling.research.invest.daily import run_daily
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.report import _money, _pct, write_dashboard
from sterling.research.invest.schema import CASH, FUNDS, allocation_json

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
    assert again.count('"record_type":"fill"') == fills
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


def test_daily_code_does_not_call_a_model() -> None:
    daily = (ROOT / "src/sterling/research/invest/daily.py").read_text(encoding="utf-8")
    script = (ROOT / "scripts/run_invest_daily.py").read_text(encoding="utf-8")
    workflow = (ROOT / "deploy/private-runner/.github/workflows/daily-invest.yml").read_text(encoding="utf-8")
    assert "xai_client" not in daily and "MockGrok" not in daily
    assert "xai_client" not in script
    assert "if: false" in workflow
    assert "cron:" not in workflow
    assert "XAI_API_KEY" not in workflow


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

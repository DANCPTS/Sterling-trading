"""Read-only dashboard: verified logs, escaped text, no order controls."""

from __future__ import annotations

import http.client
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.invest.alerts import AlertDedup, format_alert
from sterling.research.invest.book import Book
from sterling.research.invest.dashboard import _london, render_dashboard
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.preview import build_synthetic_log, main
from sterling.research.invest.report import write_dashboard
from sterling.research.invest.schema import CASH, FUNDS, allocation_json
from sterling.research.invest.verify import ledger_warnings, verify_invest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")


def test_london_clock_uses_gmt_and_bst() -> None:
    assert _london("2026-11-12T16:30:00+00:00") == "12 Nov 2026 16:30 GMT"
    assert _london("2026-07-15T15:00:00+00:00") == "15 Jul 2026 16:00 BST"


def test_page_escapes_marks_lane_and_has_no_order_controls(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "mock.jsonl"
    _decide(log, "<script>alert(1)</script>")
    page = tmp_path / "dashboard.html"
    assert write_dashboard(log, page) == 0
    html = page.read_text(encoding="utf-8")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "MOCK / PAPER" in html
    assert "GENUINE PAPER" not in html
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in html
    assert "@media (max-width: 720px)" in html
    assert "Sterling FX: not built yet" in html
    assert "Yahoo market prices (delayed, not exchange-verified)" in html
    assert "No AI explanation recorded" in html
    assert "MOCK" in html
    assert "<button" not in html and "<form" not in html and "place_order" not in html
    assert "9,955.14" not in html and "9955.14" not in html
    assert "sterling-invest" in html


def test_genuine_lane_is_not_labelled_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "forward")
    log = tmp_path / "forward.jsonl"
    _decide(log, "paper")
    destination = tmp_path / "page.html"
    assert write_dashboard(log, destination) == 0
    html = destination.read_text(encoding="utf-8")
    assert "GENUINE PAPER" in html
    assert "MOCK / PAPER" not in html
    assert "MOCK" not in html


def test_empty_log_and_tamper_keep_the_last_page(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "empty.jsonl"
    ProposalLog(log).create(now=datetime(2026, 11, 12, 12, 0, tzinfo=UTC))
    page = tmp_path / "dashboard.html"
    assert write_dashboard(log, page) == 0
    html = page.read_text(encoding="utf-8")
    assert "No verified marks yet" in html
    assert "9955.14" not in html
    kept = html
    log.write_text(log.read_text(encoding="utf-8").replace("paper-v2", "paper-v9", 1), encoding="utf-8")
    assert write_dashboard(log, page) == 1
    assert page.read_text(encoding="utf-8") == kept


def test_pending_interest_is_not_zero_and_orders_expire(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "synthetic.jsonl"
    build_synthetic_log(log)
    page = tmp_path / "dashboard.html"
    assert write_dashboard(log, page) == 0
    html = page.read_text(encoding="utf-8")
    assert "SYNTHETIC" in html
    assert "Not accrued and not zero" in html
    assert "EXPIRED" in html
    assert "pending, not zero" in html or "SONIA interest pending" in html
    assert "9,955.14" not in html


def test_synthetic_page_is_not_labelled_as_yahoo(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "synthetic.jsonl"
    build_synthetic_log(log)
    page = tmp_path / "dashboard.html"
    assert write_dashboard(log, page) == 0
    html = page.read_text(encoding="utf-8")
    assert "SYNTHETIC fixture" in html
    assert "Yahoo market prices" not in html
    assert "£10,000.00" in html
    assert "does not match" not in html


def test_decision_is_pending_before_a_skip() -> None:
    html = render_dashboard(
        (
            {
                "record_type": "decision",
                "evidence_lane": "mock",
                "system": "sterling-invest",
                "gate": "accepted",
                "timestamp_utc": "2026-11-11T18:00:00+00:00",
                "cash_gbp": "10000.00",
                "derived_actions": {"VWRP": "BUY", "IGLT": "HOLD"},
            },
        )
    )
    assert "Pending VWRP: decision BUY, not yet filled" in html
    assert "Pending IGLT" not in html
    assert "No pending order" not in html


def test_mismatched_return_is_shown_not_hidden() -> None:
    html = render_dashboard(
        (
            {
                "record_type": "decision",
                "evidence_lane": "mock",
                "system": "sterling-invest",
                "gate": "accepted",
                "timestamp_utc": "2026-11-11T18:00:00+00:00",
                "cash_gbp": "10000.00",
                "derived_actions": {},
            },
            {
                "record_type": "valuation",
                "session": "2026-11-12",
                "timestamp_utc": "2026-11-12T16:30:00+00:00",
                "nav_gbp": "10349.69",
                "cash_gbp": "10349.69",
                "daily_pnl_gbp": "349.69",
                "cumulative_return": "0.0283",
                "drawdown": "0",
                "high_water_gbp": "10349.69",
                "holdings": {},
                "prices": {},
                "data_label": "SYNTHETIC",
            },
        )
    )
    assert "does not match NAV against starting capital" in html
    assert "3.50%" in html
    assert "2.83%" in html
    assert "Yahoo market prices" not in html


def test_verifier_flags_an_old_return_without_rejecting_the_log() -> None:
    records = (
        {
            "system": "sterling-invest",
            "experiment": "paper-v2",
            "evidence_lane": "mock",
            "record_type": "decision",
            "gate": "accepted",
            "cash_gbp": "10000.00",
            "derived_actions": {"VWRP": "BUY"},
        },
        {
            "system": "sterling-invest",
            "experiment": "paper-v2",
            "evidence_lane": "mock",
            "record_type": "valuation",
            "session": "2026-11-12",
            "nav_gbp": "10349.69",
            "cash_gbp": "10349.69",
            "daily_pnl_gbp": "349.69",
            "cumulative_return": "0.0283",
            "drawdown": "0",
            "high_water_gbp": "10349.69",
            "holdings": {},
            "prices": {},
        },
    )
    verify_invest(records, lane="mock")
    notes = ledger_warnings(records)
    assert any("does not match NAV against starting capital" in note for note in notes)
    assert any("3.50%" in note and "2.83%" in note for note in notes)


def test_calendar_marks_weekends_as_no_session(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "synthetic.jsonl"
    build_synthetic_log(log)
    html = tmp_path / "page.html"
    assert write_dashboard(log, html) == 0
    text = html.read_text(encoding="utf-8")
    assert "No session" in text
    assert "Awaiting session" in text
    assert "not real-time" in text
    assert "Portfolio equity from starting capital" in text
    assert "<polyline" in text
    assert "November 2026" in text
    assert 'data-label="Price"' in text
    assert "tabular-nums" in text
    assert "£" in text
    from sterling.research.fx.grok_forward import ProposalLog

    notes = ledger_warnings(ProposalLog(log).records())
    assert notes == []
    assert "reconstructed from older evidence" not in text


def test_renderer_escapes_a_reason() -> None:
    html = render_dashboard(
        (
            {
                "record_type": "decision",
                "evidence_lane": "mock",
                "system": "sterling-invest",
                "timestamp_utc": "2026-11-12T16:30:00+00:00",
                "gate": "accepted",
                "reasons": ["<img src=x onerror=alert(1)>"],
                "prompt_text": "",
            },
        )
    )
    assert "<img" not in html
    assert "&lt;img" in html


def test_alerts_are_offline_and_deduped() -> None:
    source = (ROOT / "src/sterling/research/invest/alerts.py").read_text(encoding="utf-8")
    assert "urllib" not in source and "requests" not in source and "socket" not in source
    record = {"record_type": "fill", "record_hash": "ab" * 32, "side": "BUY", "fund": "VWRP", "session": "2026-11-12"}
    dedup = AlertDedup()
    assert dedup.accept(record) == format_alert(record)
    assert dedup.accept(record) is None
    assert format_alert({"record_type": "fill_skipped", "reason": "opening_price_unavailable"}) is None


def test_preview_refuses_a_public_host_and_serves_loopback(tmp_path: Path, stamped: None) -> None:
    del stamped
    assert main(["--host", "0.0.0.0", "--no-serve", "--out", str(tmp_path / "x.html")]) == 2
    log = tmp_path / "synthetic.jsonl"
    out = tmp_path / "dashboard.html"
    assert main(["--log", str(log), "--out", str(out), "--no-serve"]) == 0
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    thread = threading.Thread(
        target=main,
        args=(["--import-log", str(log), "--out", str(out), "--port", str(port)],),
        daemon=True,
    )
    thread.start()
    body = _fetch(port)
    assert b"SYNTHETIC" in body
    assert b"<button" not in body
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("POST", "/order", b"")
    assert connection.getresponse().status == 405


def _fetch(port: int) -> bytes:
    last = b""
    for _ in range(40):
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            connection.request("GET", "/")
            response = connection.getresponse()
            last = response.read()
            if response.status == 200:
                return last
        except OSError:
            time.sleep(0.05)
    return last


def _decide(log: Path, prompt: str) -> None:
    weights = {name: format(Decimal(1) / Decimal(7), "f") for name in (*FUNDS, CASH)}
    run_invest_week(
        log_path=log,
        book=Book(),
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=datetime(2026, 11, 12, 16, 30, tzinfo=UTC),
        provider=MockGrokProvider(steps=(ScriptStep(raw_response=allocation_json(weights)),), response_format="allocation"),
        prompt=prompt,
        mode="simulated_clock",
    )

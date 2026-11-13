"""Offline pilot: point-in-time inputs, fail-closed risk, and a dry-run request."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.test_xai_client import KEY, HttpResponse, _client, _Fake, _ok

from sterling.research.calendar import xlon
from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.invest.benchmarks import POINT_IN_TIME_CALCULATION, benchmark_table, point_in_time_path
from sterling.research.invest.book import Book
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.prompt import PROMPT_VERSION, allocation_prompt
from sterling.research.invest.recorded import RecordedAllocationProvider
from sterling.research.invest.schema import CASH, FUNDS, allocation_json, parse_allocation
from sterling.research.invest.snapshot import build_invest_snapshot
from sterling.research.invest.verify import ledger_warnings
from sterling.research.money import ZERO
from sterling.research.risk_config import load_research_limits
from sterling.research.risk_gate import apply_risk_gate, fail_closed_allocation, paper_asset_classes
from sterling.research.strategies.trend import TrendFilter
from sterling.xai_client import (
    PILOT_COST_CEILING_USD,
    XaiClient,
    XaiError,
    XaiTimeout,
    format_dry_run_report,
    pre_call_cost_usd,
)

ROOT = Path(__file__).resolve().parents[3]
AS_OF = datetime(2026, 11, 11, 16, 30, tzinfo=UTC)
WHEN = datetime(2026, 11, 2, 16, 30, tzinfo=UTC)


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")
    monkeypatch.delenv("XAI_API_KEY", raising=False)


def test_snapshot_drops_future_prices_and_unpublished_sonia(stamped: None) -> None:
    del stamped
    book = Book()
    book.quantities["VWRP"] = Decimal("10")
    book.cash = Decimal("5000.00")
    book.cumulative_costs = Decimal("1.25")
    prints = _prints()
    future = ("VWRP", date(2026, 11, 12), Decimal("999"), AS_OF + timedelta(days=1))
    sonia = ((date(2026, 11, 10), Decimal("4.00")), (date(2026, 11, 12), Decimal("9.00")))
    base = build_invest_snapshot(book=book, prints=prints, as_of=AS_OF, requested_tradable=set(FUNDS), sonia=sonia[:1])
    leaked = build_invest_snapshot(
        book=book,
        prints=[*prints, future],
        as_of=AS_OF,
        requested_tradable=set(FUNDS),
        sonia=sonia,
    )
    assert leaked == base
    text = json.dumps(base)
    assert "999" not in text
    assert "9.00" not in text
    assert base["data_availability"]["invented_observations"] == "none"
    assert base["data_availability"]["rule"] == "published_at <= as_of"
    assert base["data_availability"]["publication"] == "source-timestamp"
    assert base["data_availability"]["valuation"] == "complete"
    holdings = {row["instrument_id"]: row for row in base["portfolio"]["holdings"]}
    assert holdings["IGLT"]["staleness"] == "stale"
    assert holdings["IGLT"]["tradable"] == "false"
    assert holdings["VWRP"]["tradable"] == "true"
    assert holdings["VWRP"]["published_at"] <= AS_OF.isoformat()
    missing = build_invest_snapshot(
        book=book,
        prints=[item for item in prints if item[0] != "SGLN"],
        as_of=AS_OF,
        requested_tradable=set(FUNDS),
        sonia=(),
    )
    absent = {row["instrument_id"]: row for row in missing["portfolio"]["holdings"]}
    assert absent["SGLN"]["staleness"] == "missing"
    assert absent["SGLN"]["tradable"] == "false"
    assert absent["SGLN"]["price_gbp"] is None
    assert base["risk_constraints"]["owner_approved"] == "false"
    assert base["risk_constraints"]["operational"] == "false"
    assert base["portfolio"]["transaction_costs_gbp"] == "1.25"
    assert base["sonia"]["status"] == "published"
    assert base["sonia"]["sonia_percent"] == "4.00"
    prompt = allocation_prompt(book=book, prints=[*prints, future], as_of=AS_OF, tradable=set(FUNDS), sonia=sonia)
    assert "999" not in prompt
    assert PROMPT_VERSION in prompt


def test_hold_no_trade_model_mismatch_and_malformed(tmp_path: Path, stamped: None) -> None:
    del stamped
    cash = {name: "0" for name in FUNDS}
    cash[CASH] = "1"
    held = _run(tmp_path / "hold.jsonl", allocation_json(cash, decision="HOLD", change_justification="nothing new"))
    assert held["gate"] == "hold"
    assert held["book"].pending is None
    assert "explicit_hold" in _decision(held["log"])["reasons"]
    quiet = _run(tmp_path / "quiet.jsonl", allocation_json(cash, decision="NO_TRADE", change_justification="stand aside"))
    assert quiet["gate"] == "hold"
    assert "explicit_no_trade" in _decision(quiet["log"])["reasons"]
    shifted = {name: "0" for name in (*FUNDS, CASH)}
    shifted["VWRP"] = "0.30"
    shifted[CASH] = "0.70"
    inconsistent = _run(tmp_path / "mix.jsonl", allocation_json(shifted, decision="HOLD"))
    assert inconsistent["gate"] == "hold"
    assert inconsistent["book"].pending is None
    assert _decision(inconsistent["log"])["reasons"] == ["inconsistent_hold"]
    liar = _run(
        tmp_path / "liar.jsonl",
        allocation_json(_accepted_weights(), model_id="grok-4.3"),
        model_id="mock-grok",
    )
    assert liar["gate"] == "hold"
    assert liar["book"].pending is None
    assert _decision(liar["log"])["reasons"] == ["model_id_mismatch"]
    broken = parse_allocation(allocation_json(_accepted_weights())[:-1])
    assert broken.reason == "malformed_json"
    extra = json.loads(allocation_json(_accepted_weights()))
    extra["action"] = "BUY"
    assert parse_allocation(json.dumps(extra)).reason == "unknown_field"


def test_accepted_allocation_sets_pending_and_stores_the_snapshot(tmp_path: Path, stamped: None) -> None:
    del stamped
    result = _run(tmp_path / "accepted.jsonl", allocation_json(_accepted_weights()))
    assert result["gate"] == "accepted"
    assert result["book"].pending["VWRP"] == Decimal("0.30")
    assert result["book"].quantity("VWRP") == 0
    decision = _decision(result["log"])
    assert decision["prompt_version"] == PROMPT_VERSION
    assert len(str(decision["prompt_sha256"])) == 64
    assert decision["execution"] == "next_session_open"
    assert decision["execution_permission"] == "NOT_ISSUED"
    assert decision["risk_policy_owner_approved"] == "false"
    assert decision["eligible_session"] == "2026-11-03"
    snapshot = decision["research_snapshot"]
    assert snapshot["portfolio"]["cash_gbp"] == "10000.00"
    assert snapshot["data_availability"]["rule"] == "published_at <= as_of"
    assert "999" not in json.dumps(snapshot)


def test_rejections_do_not_activate_or_clear_pending(tmp_path: Path, stamped: None) -> None:
    del stamped
    limits = load_research_limits()
    assert limits.owner_approved is False
    classes = paper_asset_classes()
    concentrated = _run(tmp_path / "concentrated.jsonl", allocation_json(_weight("VWRP", "0.70")))
    assert concentrated["gate"] == "hold"
    assert concentrated["book"].pending is None
    assert any("concentration" in reason for reason in _decision(concentrated["log"])["reasons"])
    clipped = apply_risk_gate({"VWRP": Decimal("0.70")}, {}, limits, classes, Decimal("10000"))
    assert clipped.clipped is True
    assert clipped.weight_map()["VWRP"] == Decimal("0.50")
    closed = fail_closed_allocation(
        {"VWRP": Decimal("0.70"), CASH: Decimal("0.30")},
        {name: ZERO for name in FUNDS},
        limits,
        classes,
        Decimal("10000"),
        tradable=set(FUNDS),
        stale=set(),
        priced=set(FUNDS),
        eligible=set(FUNDS),
        decision_at=WHEN,
        fill_session=date(2026, 11, 3),
    )
    assert closed.accepted is False
    gold = _run(tmp_path / "gold.jsonl", allocation_json(_weight("SGLN", "0.25")))
    assert any("commodity" in reason for reason in _decision(gold["log"])["reasons"])
    assert gold["book"].pending is None
    crowded = Book()
    crowded.quantities["VWRP"] = Decimal("40")
    crowded.quantities["CUKX"] = Decimal("40")
    crowded.cash = Decimal("2000.00")
    rotated = {name: "0" for name in (*FUNDS, CASH)}
    rotated["IGLT"] = "0.40"
    rotated["AGBP"] = "0.40"
    rotated[CASH] = "0.20"
    prior = {name: ZERO for name in (*FUNDS, CASH)}
    prior["VWRP"] = Decimal("0.40")
    prior[CASH] = Decimal("0.60")
    crowded.pending = prior
    outcome = _run(tmp_path / "turnover.jsonl", allocation_json(rotated), book=crowded)
    assert outcome["gate"] == "hold"
    assert any("turnover" in reason for reason in _decision(outcome["log"])["reasons"])
    assert outcome["book"].pending == prior
    assert "EXPIRED" not in outcome["log"].read_text(encoding="utf-8")
    cash_breach = fail_closed_allocation(
        {"VWRP": Decimal("0.80"), CASH: Decimal("0.40")},
        {name: ZERO for name in FUNDS},
        limits,
        classes,
        Decimal("10000"),
        tradable=set(FUNDS),
        stale=set(),
        priced=set(FUNDS),
        eligible=set(FUNDS),
        decision_at=WHEN,
        fill_session=date(2026, 11, 3),
    )
    assert any(reason.startswith("rejected: cash") or "gross" in reason for reason in cash_breach.reasons)
    foreign = fail_closed_allocation(
        {"CSP1": Decimal("0.10"), CASH: Decimal("0.90")},
        {name: ZERO for name in FUNDS},
        limits,
        classes,
        Decimal("10000"),
        tradable=set(FUNDS),
        stale=set(),
        priced=set(FUNDS),
        eligible=set(FUNDS),
        decision_at=WHEN,
        fill_session=date(2026, 11, 3),
    )
    assert any("instrument_eligibility" in reason for reason in foreign.reasons)


def test_stale_missing_and_same_session_fills_are_rejected(tmp_path: Path, stamped: None) -> None:
    del stamped
    stale_prints = _prints(as_of=AS_OF)
    wanted = {name: "0" for name in (*FUNDS, CASH)}
    wanted["IGLT"] = "0.20"
    wanted[CASH] = "0.80"
    stale = _run(tmp_path / "stale.jsonl", allocation_json(wanted), prints=stale_prints, as_of=AS_OF)
    assert stale["gate"] == "hold"
    assert stale["book"].pending is None
    assert any("stale_or_untradable" in reason for reason in _decision(stale["log"])["reasons"])
    blind = Book()
    blind.quantities["VWRP"] = Decimal("10")
    prices = {name: Decimal("100") for name in FUNDS if name != "VWRP"}
    missing = _run(tmp_path / "missing.jsonl", allocation_json(_accepted_weights()), book=blind, prices=prices)
    assert missing["gate"] == "hold"
    assert missing["book"].pending is None
    assert missing["book"].quantity("VWRP") == Decimal("10")
    assert "missing_valuation" in _decision(missing["log"])["reasons"]
    early = _run(tmp_path / "same-session.jsonl", allocation_json(_accepted_weights()), fill_session=WHEN.date())
    assert early["gate"] == "hold"
    assert early["book"].pending is None
    assert any("next_open_timing" in reason for reason in _decision(early["log"])["reasons"])


def test_future_market_and_sonia_do_not_change_earlier_results() -> None:
    ends = list(xlon().month_ends(date(2024, 1, 1), date(2025, 6, 30)))
    early_days = ends[:12]
    early = _series(early_days, crash_after=None)
    extended = _series(ends, crash_after=early_days[-1])
    strategy = TrendFilter(lookback_months=10, defensive=None)
    before = point_in_time_path(strategy, early, early_days, ())
    after = point_in_time_path(strategy, extended, ends, ())
    assert before == after[: len(before)]
    assert any(Decimal(weights.get("VWRP", "0")) > 0 for _day, _nav, weights in before if weights)
    altered = _series(early_days, crash_after=None)
    last = early_days[-1]
    altered = {
        ticker: tuple((day, Decimal("1") if day == last else price) for day, price in points) for ticker, points in altered.items()
    }
    changed = point_in_time_path(strategy, altered, early_days, ())
    assert changed[:-1] == before[:-1]
    assert changed[-1] != before[-1]
    sessions = [date(2025, 3, 7)]
    published = ((date(2025, 3, 6), Decimal("4.00")),)
    future = (*published, (date(2025, 3, 7), Decimal("9.00")))
    ceiling = datetime(2025, 3, 31, 16, 30, tzinfo=UTC)
    known = benchmark_table({}, sessions, sonia=published, as_of=ceiling)
    leaked = benchmark_table({}, sessions, sonia=future, as_of=ceiling)
    assert known["benchmark_calculation"] == POINT_IN_TIME_CALCULATION
    assert known["benchmark_status"] == "valid"
    assert "need" in known["legacy_benchmark_rule"] and "recalculation" in known["legacy_benchmark_rule"]
    known_cash = _arm(known, "sterling_cash")["end_nav_gbp"]
    assert known_cash == _arm(leaked, "sterling_cash")["end_nav_gbp"]
    assert Decimal(str(known_cash)) > Decimal("10000")
    rich = benchmark_table({}, sessions, sonia=((date(2025, 3, 6), Decimal("9.00")),), as_of=ceiling)
    assert Decimal(str(_arm(rich, "sterling_cash")["end_nav_gbp"])) > Decimal(str(known_cash))
    legacy = ledger_warnings(
        (
            {
                "record_type": "performance",
                "session": "2026-11-12",
                "arms": [
                    {"name": "sterling_cash", "end_nav_gbp": "10000.00", "cumulative_return": "0", "start_nav_gbp": "10000.00"}
                ],
            },
        )
    )
    assert any("point-in-time-v1" in note and "not rewritten" in note for note in legacy)


def test_dry_run_report_and_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", KEY)
    prompt = allocation_prompt(book=Book(), prints=_prints(as_of=WHEN), as_of=WHEN, tradable=set(FUNDS))
    client = XaiClient(enabled=False, dry_run=True, model="grok-4.3")
    text = format_dry_run_report(client.dry_run_report(prompt))
    assert "dry_run: true" in text
    assert "sent: false" in text
    assert "model: grok-4.3" in text
    assert "endpoint: https://api.x.ai/v1/chat/completions" in text
    assert "Authorization: Bearer [redacted]" in text
    assert KEY not in text
    assert "retries_after_send: 0" in text
    assert "cost_ceiling_usd: 0.25" in text
    assert "price_input_usd_per_million: 1.25" in text
    assert "price_cached_input_usd_per_million: 0.20" in text
    assert "price_output_usd_per_million: 2.50" in text
    assert "input 2.50; cached input 0.40; output 5.00" in text
    assert "us_regional_endpoint_multiplier: 1.1" in text
    assert "not a provider billing cap" in text
    assert "full request JSON" in text
    assert "within_ceiling: true" in text
    assert client.estimated_cost_usd(prompt) <= PILOT_COST_CEILING_USD
    long_estimate = pre_call_cost_usd(200_000, 1500)
    assert long_estimate == Decimal("0.5075")
    denied = _Fake([HttpResponse(status=500, body=b"no"), _ok("{}")])
    with pytest.raises(XaiError, match="HTTP 500"):
        _client(denied, max_calls=1, max_attempts=3).complete("hello")
    assert len(denied.calls) == 1
    timed = _Fake(error=TimeoutError("late"))
    with pytest.raises(XaiTimeout):
        _client(timed, max_attempts=3).complete("hello")
    assert len(timed.calls) == 1


def test_recorded_forward_lane_stays_in_tmp(tmp_path: Path, stamped: None, monkeypatch: pytest.MonkeyPatch) -> None:
    del stamped
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "forward")
    log = tmp_path / "evidence" / "invest-v2" / "forward.jsonl"
    raw = allocation_json(_accepted_weights(), model_id="grok-4.3")
    result = run_invest_week(
        log_path=log,
        book=Book(),
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=WHEN,
        provider=RecordedAllocationProvider(raw, model_id="grok-4.3"),
        prompt="recorded",
        mode="simulated_clock",
    )
    assert result["gate"] == "accepted"
    assert result["provider_calls"] == 1
    assert log.is_file()
    assert not (ROOT / "evidence" / "invest-v2" / "forward.jsonl").exists()
    assert not (ROOT / "evidence" / "invest-v2" / "mock" / "forward.jsonl").exists()
    script = ROOT / "scripts" / "run_invest_v2.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["STERLING_EVIDENCE_LANE"] = "forward"
    env.pop("XAI_API_KEY", None)
    dry = tmp_path / "nested" / "evidence" / "invest-v2" / "forward.jsonl"
    completed = subprocess.run(
        [sys.executable, str(script), "--log", str(dry), "--dry-run", "--fixture", "--as-of", WHEN.isoformat()],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert "sent: false" in completed.stdout
    assert "cost_ceiling_usd: 0.25" in completed.stdout
    assert not dry.exists()
    refused = subprocess.run(
        [
            sys.executable,
            str(script),
            "--log",
            str(ROOT / "evidence" / "invest-v2" / "forward.jsonl"),
            "--dry-run",
            "--fixture",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert refused.returncode == 2
    assert "repository evidence" in refused.stderr
    assert not (ROOT / "evidence" / "invest-v2" / "forward.jsonl").exists()


def _prints(as_of: datetime = AS_OF) -> list[tuple[str, date, Decimal, datetime]]:
    fresh = as_of
    old = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)
    rows = []
    for name in FUNDS:
        when = old if name == "IGLT" else fresh
        session = date(2026, 10, 1) if name == "IGLT" else as_of.date()
        rows.append((name, session, Decimal("100"), when))
    return rows


def _accepted_weights() -> dict[str, str]:
    weights = {name: "0" for name in (*FUNDS, CASH)}
    weights["VWRP"] = "0.30"
    weights["IGLT"] = "0.20"
    weights["XSTR"] = "0.10"
    weights[CASH] = "0.40"
    return weights


def _weight(ticker: str, amount: str) -> dict[str, str]:
    weights = {name: "0" for name in (*FUNDS, CASH)}
    weights[ticker] = amount
    weights[CASH] = format(Decimal(1) - Decimal(amount), "f")
    return weights


def _run(
    log: Path,
    raw: str,
    *,
    book: Book | None = None,
    prices: dict[str, Decimal] | None = None,
    prints: list[tuple[str, date, Decimal, datetime]] | None = None,
    as_of: datetime = WHEN,
    fill_session: date | None = None,
    model_id: str = "mock-grok",
) -> dict[str, object]:
    held = Book() if book is None else book
    result = run_invest_week(
        log_path=log,
        book=held,
        prices={name: Decimal("100") for name in FUNDS} if prices is None else prices,
        tradable=set(FUNDS),
        as_of=as_of,
        provider=MockGrokProvider(steps=(ScriptStep(raw_response=raw, model_id=model_id),), response_format="allocation"),
        prompt="paper-v2",
        prints=prints,
        fill_session=fill_session,
    )
    return {"book": held, "gate": result["gate"], "log": log, "result": result}


def _decision(log: Path) -> dict[str, object]:
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return next(row for row in rows if row["record_type"] == "decision")


def _series(days: list[date], crash_after: date | None) -> dict[str, tuple[tuple[date, Decimal], ...]]:
    closes: dict[str, tuple[tuple[date, Decimal], ...]] = {}
    for ticker in FUNDS:
        points = []
        for index, day in enumerate(days):
            price = Decimal("1") if crash_after is not None and day > crash_after else Decimal(100 + index)
            points.append((day, price))
        closes[ticker] = tuple(points)
    return closes


def _arm(table: dict[str, object], name: str) -> dict[str, object]:
    arms = table["arms"]
    assert isinstance(arms, list)
    return next(arm for arm in arms if isinstance(arm, dict) and arm["name"] == name)

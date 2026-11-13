"""Regressions for the PR #15 review: one snapshot, stale holdings, NAV, benchmarks, dry-run, failures."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from scripts.run_invest_v2 import main

from sterling.research.grok.providers import ProviderEnvelope
from sterling.research.invest.benchmarks import benchmark_table, frozen_book, point_in_time_path
from sterling.research.invest.book import Book, declare_distribution, valuation_of
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.schema import CASH, FUNDS, allocation_json
from sterling.research.invest.snapshot import build_invest_snapshot
from sterling.research.strategies.trend import TrendFilter
from sterling.xai_client import XaiError, XaiTimeout

AS_OF = datetime(2026, 11, 11, 16, 30, tzinfo=UTC)
WHEN = datetime(2026, 11, 2, 16, 30, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")
    monkeypatch.delenv("XAI_API_KEY", raising=False)


def test_cli_keeps_an_old_quote_stale_and_drops_the_future(
    tmp_path: Path, stamped: None, capsys: pytest.CaptureFixture[str]
) -> None:
    del stamped
    cache = _cache(tmp_path)
    log = tmp_path / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    reply = tmp_path / "reply.json"
    reply.write_text(allocation_json(_buy("IGLT", "0.20"), model_id="grok-4.3"), encoding="utf-8")
    code = main(
        [
            "--log",
            str(log),
            "--provider",
            "recorded",
            "--response-file",
            str(reply),
            "--cache",
            str(cache),
            "--as-of",
            AS_OF.isoformat(),
            "--simulated-clock",
        ]
    )
    assert code == 0
    decision = _decision(log)
    prompt = json.loads(str(decision["prompt_text"]))
    assert prompt["research_snapshot"] == decision["research_snapshot"]
    holdings = {row["instrument_id"]: row for row in decision["research_snapshot"]["portfolio"]["holdings"]}
    assert holdings["IGLT"]["staleness"] == "stale"
    assert holdings["IGLT"]["tradable"] == "false"
    assert holdings["IGLT"]["published_at"] == "2026-10-01T16:30:00+00:00"
    assert holdings["IGLT"]["publication"] == "source-timestamp"
    text = json.dumps(decision["research_snapshot"])
    assert "999" not in text
    assert decision["gate"] == "hold"
    assert any("stale_or_untradable" in reason for reason in decision["reasons"])
    other = tmp_path / "other" / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    later_cache = _cache(tmp_path / "later", future=False)
    reply.write_text(allocation_json(_legal(), model_id="grok-4.3"), encoding="utf-8")
    assert (
        main(
            [
                "--log",
                str(other),
                "--provider",
                "recorded",
                "--response-file",
                str(reply),
                "--cache",
                str(later_cache),
                "--as-of",
                AS_OF.isoformat(),
                "--simulated-clock",
            ]
        )
        == 0
    )
    with_future = _decision(log)
    without = _decision(other)
    assert "999" not in json.dumps(without["research_snapshot"])
    assert with_future["research_snapshot"]["price_history"] == _history_without_future(without)
    captured = capsys.readouterr()
    assert "999" not in captured.out


def test_synthetic_fixture_dry_run(tmp_path: Path, stamped: None, capsys: pytest.CaptureFixture[str]) -> None:
    del stamped
    log = tmp_path / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    assert main(["--log", str(log), "--dry-run", "--fixture", "--as-of", WHEN.isoformat()]) == 0
    text = capsys.readouterr().out.replace('\\"', '"')
    assert "data_source: synthetic-fixture" in text
    assert "publication: synthetic" in text
    assert '"source":"synthetic-fixture"' in text
    assert not log.exists()


def test_stale_holding_blocks_another_purchase_and_hold_still_works(tmp_path: Path, stamped: None) -> None:
    del stamped
    book = Book()
    book.quantities["VWRP"] = Decimal("10")
    book.cash = Decimal("9000.00")
    book.pending = {name: Decimal("0") for name in (*FUNDS, CASH)}
    book.pending["VWRP"] = Decimal("0.10")
    book.pending[CASH] = Decimal("0.90")
    prints = _prints(stale="VWRP")
    bought = _run(tmp_path / "buy.jsonl", allocation_json(_buy("CUKX", "0.20")), book=book, prints=prints, as_of=AS_OF)
    assert bought["result"]["gate"] == "hold"
    assert any("stale_holding" in reason for reason in _decision(bought["log"])["reasons"])
    assert bought["book"].pending["VWRP"] == Decimal("0.10")
    assert "EXPIRED" not in bought["log"].read_text(encoding="utf-8")
    missing = Book()
    missing.quantities["VWRP"] = Decimal("10")
    missing.cash = Decimal("9000.00")
    blind = _run(
        tmp_path / "blind.jsonl",
        allocation_json(_legal()),
        book=missing,
        prices={name: Decimal("100") for name in FUNDS if name != "VWRP"},
    )
    assert "missing_valuation" in _decision(blind["log"])["reasons"]
    assert blind["book"].quantity("VWRP") == Decimal("10")
    held = Book()
    held.quantities["VWRP"] = Decimal("10")
    held.cash = Decimal("9000.00")
    current = valuation_of(held, {name: Decimal("100") for name in FUNDS})
    assert current is not None
    weights = {name: format(current[name], "f") for name in (*FUNDS, CASH)}
    quiet = _run(
        tmp_path / "hold.jsonl",
        allocation_json(weights, decision="HOLD", change_justification="keep"),
        book=held,
        prints=prints,
        as_of=AS_OF,
    )
    assert _decision(quiet["log"])["reasons"] == ["explicit_hold"]
    quiet_trade = _run(
        tmp_path / "no-trade.jsonl",
        allocation_json(weights, decision="NO_TRADE", change_justification="stand aside"),
        book=held,
        prints=prints,
        as_of=AS_OF,
    )
    assert _decision(quiet_trade["log"])["reasons"] == ["explicit_no_trade"]
    fresh = _run(tmp_path / "fresh.jsonl", allocation_json(_legal()), prints=_prints(stale=None), as_of=AS_OF)
    assert fresh["result"]["gate"] == "accepted"


def test_receivables_are_in_nav_and_the_snapshot_does_not_move_the_peak(tmp_path: Path, stamped: None) -> None:
    del stamped
    book = Book()
    book.cash = Decimal("5000.00")
    book.quantities["VWRP"] = Decimal("40")
    book.peak = Decimal("10000.00")
    book.max_drawdown = Decimal("0.01")
    book.nav_rows.append((date(2026, 11, 10), Decimal("10000.00")))
    declare_distribution(book, "VWRP", Decimal("50.00"), date(2026, 11, 20))
    prices = {name: Decimal("100") for name in FUNDS}
    valued = valuation_of(book, prices)
    assert valued is not None
    assert valued["nav"] == Decimal("9050.00")
    assert valued["spendable_cash"] == Decimal("5000.00")
    assert valued["receivable"] == Decimal("50.00")
    assert abs(valued["VWRP"] + valued[CASH] + valued["RECEIVABLE"] - 1) < Decimal("0.0000001")
    before = (book.peak, book.max_drawdown, list(book.nav_rows), book.cash)
    snapshot = build_invest_snapshot(
        book=book,
        prints=[(name, AS_OF.date(), Decimal("100"), AS_OF) for name in FUNDS],
        as_of=AS_OF,
        requested_tradable=set(FUNDS),
    )
    assert (book.peak, book.max_drawdown, list(book.nav_rows), book.cash) == before
    assert snapshot["portfolio"]["nav_gbp"] == "9050.00"
    assert snapshot["portfolio"]["spendable_cash_gbp"] == "5000.00"
    assert snapshot["portfolio"]["receivable_gbp"] == "50.00"
    assert snapshot["portfolio"]["cash_gbp"] == "5000.00"
    result = _run(tmp_path / "nav.jsonl", allocation_json(_legal()), book=book, prints=_prints(stale=None), as_of=AS_OF)
    assert result["result"]["nav_gbp"] == "9050.00"
    assert _decision(result["log"])["research_snapshot"]["portfolio"]["nav_gbp"] == "9050.00"
    assert (book.peak, book.max_drawdown, list(book.nav_rows)) == before[:3]


def test_benchmarks_label_execution_and_ignore_unpublished_closes() -> None:
    from sterling.research.calendar import xlon

    ends = list(xlon().month_ends(date(2024, 1, 1), date(2024, 12, 31)))
    closes = {name: tuple((day, Decimal(100 + index)) for index, day in enumerate(ends)) for name in FUNDS}
    signal = ends[9]
    hidden = datetime(signal.year, signal.month, signal.day, 18, 0, tzinfo=UTC)
    visible = point_in_time_path(TrendFilter(lookback_months=10), closes, ends, ())
    blocked = point_in_time_path(
        TrendFilter(lookback_months=10),
        closes,
        ends,
        (),
        publications={name: ((signal, hidden),) for name in FUNDS},
    )
    assert [row for row in blocked if row[0] < signal] == [row for row in visible if row[0] < signal]
    assert next(row for row in blocked if row[0] == signal) != next(row for row in visible if row[0] == signal)
    following = ends[10]
    partial = {name: tuple(point for point in points if point[0] != following) for name, points in closes.items()}
    untouched = frozen_book(TrendFilter(lookback_months=10), partial, ends[:11], ())
    assert untouched.trades == 0
    table = benchmark_table(closes, ends, sonia=((ends[0], Decimal("4.00")),))
    assert table["execution_equivalence"] == "not_claimed"
    assert "not fully execution-equivalent" in str(table["execution_note"])
    conventions = {arm["execution_convention"] for arm in table["arms"]}
    assert "decide_at_month_end_close_fill_next_session_close" in conventions
    assert "enter_at_first_session_close" in conventions
    assert len(conventions) > 1
    assert all(arm["open_substitution"] == "none" for arm in table["arms"])
    assert all(arm["execution_comparability"] == "provisional_not_fully_execution_equivalent" for arm in table["arms"])
    assert {arm["start_nav_gbp"] for arm in table["arms"]} == {"10000.00"}
    del signal


def test_cache_dry_run_does_not_call_or_write_evidence(
    tmp_path: Path, stamped: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    del stamped

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr("urllib.request.urlopen", _blocked)
    cache = _cache(tmp_path)
    log = tmp_path / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    assert main(["--log", str(log), "--dry-run", "--cache", str(cache), "--as-of", AS_OF.isoformat()]) == 0
    text = capsys.readouterr().out
    assert "sent: false" in text
    assert "connection: not opened" in text
    assert "orders: none" in text
    assert "data_source: supplied" in text
    assert "publication: source-timestamp" in text
    assert "IGLT" in text.split("stale: ", 1)[1].split("\n", 1)[0]
    assert '"price_gbp":"999"' not in text.replace('\\"', '"')
    assert "2026-11-12" not in text
    assert "prompt_sha256: " in text
    assert "token_estimate_scope: full request JSON" in text
    assert "not a provider billing cap" in text
    assert "cost_ceiling_usd: 0.25" in text
    assert not log.exists()
    assert not (ROOT / "evidence" / "invest-v2" / "forward.jsonl").exists()
    assert not (ROOT / "evidence" / "invest-v2" / "mock" / "forward.jsonl").exists()
    (tmp_path / "dry-run.txt").write_text(text, encoding="utf-8")


def test_provider_failures_are_audited_and_do_not_trade_again(tmp_path: Path, stamped: None) -> None:
    del stamped
    book = Book()
    book.pending = {name: Decimal("0") for name in (*FUNDS, CASH)}
    book.pending[CASH] = Decimal("1")
    http = _run_error(tmp_path / "http.jsonl", XaiError("xAI HTTP 500. upstream"), book)
    assert http["result"]["status"] == "failed"
    assert http["result"]["sent_state"] == "possibly_sent_or_billed"
    assert http["result"]["provider_calls"] == 1
    assert _failure(http["log"])["kind"] == "XaiError"
    assert http["book"].pending[CASH] == Decimal("1")
    assert '"record_type":"decision"' not in http["log"].read_text(encoding="utf-8")
    again = run_invest_week(
        log_path=http["log"],
        book=http["book"],
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=WHEN,
        provider=_Boom(XaiError("xAI HTTP 500. upstream")),
        prompt="paper-v2",
    )
    assert again["provider_calls"] == 0
    assert again["status"] == "noop"
    timed = _run_error(tmp_path / "time.jsonl", XaiTimeout("xAI read timed out after send. Not retried."))
    assert timed["result"]["sent_state"] == "possibly_sent_or_billed"
    refused = _run_error(
        tmp_path / "refused.jsonl",
        XaiError("xAI connection was refused before the request was sent.", sent_state="definitely_not_sent"),
    )
    assert refused["result"]["provider_calls"] == 0
    assert _failure(refused["log"])["sent_state"] == "definitely_not_sent"
    broken = _run(tmp_path / "json.jsonl", "not-json")
    assert _decision(broken["log"])["disposition"] == "HOLD"
    assert _failure(broken["log"])["kind"] == "invalid_response"
    mismatch = _run_reported(tmp_path / "model.jsonl", json_model="grok-4.3", reported="grok-other")
    reasons = _decision(mismatch["log"])["reasons"]
    assert "model_id_mismatch" in reasons
    assert "provider_model_mismatch" in reasons
    assert _decision(mismatch["log"])["provider_model_identity"] == "mismatch"
    assert mismatch["book"].pending is None


def _cache(root: Path, future: bool = True) -> Path:
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    for name in FUNDS:
        rows = ["date,close,published_at,source"]
        if name == "IGLT":
            rows.append("2026-10-01,100,2026-10-01T16:30:00+00:00,local-csv")
        else:
            rows.append("2026-11-11,110,2026-11-11T16:30:00+00:00,local-csv")
        if future and name == "VWRP":
            rows.append("2026-11-12,999,2026-11-12T16:30:00+00:00,local-csv")
        (cache / f"{name}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return cache


def _prints(stale: str | None) -> list[tuple[str, date, Decimal, datetime]]:
    old = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)
    rows = []
    for name in FUNDS:
        when = old if name == stale else AS_OF
        session = date(2026, 10, 1) if name == stale else AS_OF.date()
        rows.append((name, session, Decimal("100"), when))
    return rows


def _legal() -> dict[str, str]:
    weights = {name: "0" for name in (*FUNDS, CASH)}
    weights["VWRP"] = "0.30"
    weights["IGLT"] = "0.20"
    weights["XSTR"] = "0.10"
    weights[CASH] = "0.40"
    return weights


def _buy(ticker: str, amount: str) -> dict[str, str]:
    weights = {name: "0" for name in (*FUNDS, CASH)}
    weights[ticker] = amount
    weights[CASH] = format(Decimal("1") - Decimal(amount), "f")
    return weights


def _run(
    log: Path,
    raw: str,
    *,
    book: Book | None = None,
    prints: list[tuple[str, date, Decimal, datetime]] | None = None,
    prices: dict[str, Decimal] | None = None,
    as_of: datetime = WHEN,
) -> dict[str, object]:
    held = Book() if book is None else book
    provider = _Script(raw)
    result = run_invest_week(
        log_path=log,
        book=held,
        prices={name: Decimal("100") for name in FUNDS} if prices is None else prices,
        tradable=set(FUNDS),
        as_of=as_of,
        provider=provider,
        prompt="paper-v2",
        prints=prints,
    )
    return {"book": held, "log": log, "result": result}


def _run_error(log: Path, exc: Exception, book: Book | None = None) -> dict[str, object]:
    held = Book() if book is None else book
    result = run_invest_week(
        log_path=log,
        book=held,
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=WHEN,
        provider=_Boom(exc),
        prompt="paper-v2",
    )
    return {"book": held, "log": log, "result": result}


def _run_reported(log: Path, *, json_model: str, reported: str) -> dict[str, object]:
    held = Book()
    result = run_invest_week(
        log_path=log,
        book=held,
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=WHEN,
        provider=_Reported(allocation_json(_legal(), model_id=json_model), reported),
        prompt="paper-v2",
    )
    return {"book": held, "log": log, "result": result}


def _decision(log: Path) -> dict[str, object]:
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return next(row for row in rows if row["record_type"] == "decision")


def _failure(log: Path) -> dict[str, object]:
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return next(row for row in rows if row["record_type"] == "provider_failure")


def _history_without_future(decision: dict[str, object]) -> object:
    snapshot = decision["research_snapshot"]
    assert isinstance(snapshot, dict)
    return snapshot["price_history"]


class _Script:
    def __init__(self, raw: str) -> None:
        self._raw = raw

    def complete(self, snapshot: object, *, prompt: str) -> ProviderEnvelope:
        del prompt
        as_of = snapshot.as_of
        return ProviderEnvelope(model_id="mock-grok", responded_at=as_of, raw_response=self._raw)


class _Reported:
    def __init__(self, raw: str, reported: str) -> None:
        self._raw = raw
        self._reported = reported

    def complete(self, snapshot: object, *, prompt: str) -> ProviderEnvelope:
        del prompt
        as_of = snapshot.as_of
        return ProviderEnvelope(
            model_id="mock-grok",
            responded_at=as_of,
            raw_response=self._raw,
            provider_reported_model=self._reported,
        )


class _Boom:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    def complete(self, snapshot: object, *, prompt: str) -> ProviderEnvelope:
        del snapshot, prompt
        self.calls += 1
        raise self._exc

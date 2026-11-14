"""Compact prompts, Yahoo availability, SONIA cache lag, and one NAV."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from scripts.run_invest_v2 import main

from sterling.research.grok.quotes import normalise_yahoo_gbp
from sterling.research.invest.availability import assumed_available_at, exchange_close_at
from sterling.research.invest.book import Book, declare_distribution
from sterling.research.invest.decide import _market_snapshot, run_invest_week
from sterling.research.invest.prompt import canonical_snapshot_sha256, project_snapshot
from sterling.research.invest.schema import ALLOCATION_SCHEMA, CASH, FUNDS, allocation_json
from sterling.research.invest.snapshot import PricePrint, build_invest_snapshot, prices_from_snapshot
from sterling.xai_client import XaiClient, XaiDryRun

AS_OF = datetime(2026, 11, 12, 16, 30, tzinfo=UTC)


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")
    monkeypatch.delenv("XAI_API_KEY", raising=False)


def test_assumed_close_is_not_available_at_the_same_day_close() -> None:
    session = date(2026, 11, 11)
    close = exchange_close_at(session)
    opened = assumed_available_at(session)
    assert close == datetime(2026, 11, 11, 16, 30, tzinfo=UTC)
    assert opened == datetime(2026, 11, 12, 8, 0, tzinfo=UTC)
    row = _assumed("VWRP", session)
    for moment in (close - timedelta(seconds=1), close, opened - timedelta(seconds=1)):
        held = _holding(build_invest_snapshot(book=Book(), prints=[row], as_of=moment, requested_tradable=set(FUNDS)), "VWRP")
        assert held["price_gbp"] is None
        assert held["tradable"] == "false"
    ready = _holding(
        build_invest_snapshot(book=Book(), prints=[row], as_of=opened, requested_tradable=set(FUNDS)),
        "VWRP",
    )
    assert ready["price_gbp"] == "100"
    assert ready["tradable"] == "true"
    assert ready["availability_basis"] == "assumed-next-session"
    assert ready["provider_published_at"] is None
    assert ready["available_at"] == opened.isoformat()
    assert ready["exchange_close_at"] == close.isoformat()


def test_london_close_and_next_open_follow_british_time() -> None:
    assert exchange_close_at(date(2026, 3, 27)) == datetime(2026, 3, 27, 16, 30, tzinfo=UTC)
    assert exchange_close_at(date(2026, 3, 30)) == datetime(2026, 3, 30, 15, 30, tzinfo=UTC)
    assert assumed_available_at(date(2026, 3, 27)) == datetime(2026, 3, 30, 7, 0, tzinfo=UTC)
    assert exchange_close_at(date(2026, 10, 23)) == datetime(2026, 10, 23, 15, 30, tzinfo=UTC)
    assert exchange_close_at(date(2026, 10, 26)) == datetime(2026, 10, 26, 16, 30, tzinfo=UTC)
    assert assumed_available_at(date(2026, 10, 23)) == datetime(2026, 10, 26, 8, 0, tzinfo=UTC)


def test_cache_without_availability_metadata_fails(tmp_path: Path, stamped: None, capsys: pytest.CaptureFixture[str]) -> None:
    del stamped
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "VWRP.csv").write_text("date,close\n2026-11-11,100\n", encoding="utf-8")
    log = tmp_path / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    assert main(["--log", str(log), "--dry-run", "--cache", str(cache), "--as-of", AS_OF.isoformat()]) == 2
    assert "provider_published_at" in capsys.readouterr().err
    naive = tmp_path / "naive"
    naive.mkdir()
    (naive / "VWRP.csv").write_text(
        "date,close,published_at\n2026-11-11,100,2026-11-11T16:30:00\n",
        encoding="utf-8",
    )
    assert main(["--log", str(log), "--dry-run", "--cache", str(naive), "--as-of", AS_OF.isoformat()]) == 2
    assert "timezone" in capsys.readouterr().err


def test_a_later_session_in_the_cache_does_not_change_the_earlier_snapshot() -> None:
    early = assumed_available_at(date(2026, 11, 10))
    later = _assumed("VWRP", date(2026, 11, 12), price=Decimal("999"))
    first = build_invest_snapshot(
        book=Book(), prints=[_assumed("VWRP", date(2026, 11, 10))], as_of=early, requested_tradable=set(FUNDS)
    )
    both = build_invest_snapshot(
        book=Book(),
        prints=[_assumed("VWRP", date(2026, 11, 10)), later],
        as_of=early,
        requested_tradable=set(FUNDS),
    )
    assert first == both
    assert "999" not in json_text(first)


def test_cached_sonia_waits_for_nine_london(tmp_path: Path, stamped: None, capsys: pytest.CaptureFixture[str]) -> None:
    del stamped
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "VWRP.csv").write_text(
        "date,close,published_at\n2026-11-10,100,2026-11-10T16:30:00+00:00\n",
        encoding="utf-8",
    )
    (cache / "boe_sonia.csv").write_text("DATE,IUDSOIA\n11 Nov 2026,3.9700\n12 Nov 2026,4.1000\n", encoding="utf-8")
    log = tmp_path / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    early = "2026-11-12T08:59:59+00:00"
    assert main(["--log", str(log), "--dry-run", "--cache", str(cache), "--as-of", early]) == 0
    early_text = capsys.readouterr().out
    assert "sonia_status: unavailable" in early_text
    assert "sonia_not_yet_published" in early_text
    assert main(["--log", str(log), "--dry-run", "--cache", str(cache), "--as-of", "2026-11-12T09:00:00+00:00"]) == 0
    later = capsys.readouterr().out
    assert "sonia_status: published" in later
    assert "sonia_reference_date: 2026-11-11" in later
    assert "sonia_percent: 3.9700" in later
    assert "4.1000" not in later.split("payload: ", 1)[0]


def test_compact_prompt_stays_under_the_token_ceiling_and_the_gate_ignores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "not-a-real-key")
    prints = []
    start = date(2024, 1, 2)
    for offset in range(400):
        session = start + timedelta(days=offset)
        if session.weekday() >= 5:
            continue
        published = datetime(session.year, session.month, session.day, 16, 30, tzinfo=UTC)
        for name in FUNDS:
            prints.append((name, session, Decimal("100") + offset, published))
    snapshot = build_invest_snapshot(book=Book(), prints=prints, as_of=AS_OF, requested_tradable=set(FUNDS))
    from sterling.research.invest.prompt import allocation_prompt

    prompt = allocation_prompt(book=Book(), prints=prints, as_of=AS_OF, tradable=set(FUNDS), snapshot=snapshot)
    assert "price_history" not in json.loads(prompt)
    assert len(json.loads(prompt)["model_view"]["indicator_values"]) == 60
    client = XaiClient(enabled=True, dry_run=True, transport=_Silent())
    with pytest.raises(XaiDryRun):
        client.complete(prompt, schema=ALLOCATION_SCHEMA)
    report = client.dry_run_report(prompt, schema=ALLOCATION_SCHEMA)
    assert int(report["token_estimate"]) < 6000
    assert Decimal(str(report["pre_call_cost_estimate_usd"])) <= Decimal("0.25")
    lied = prompt.replace('"tradable":"false"', '"tradable":"true"')
    book = Book()
    result = run_invest_week(
        log_path=tmp_path / "gate.jsonl",
        book=book,
        prices=prices_from_snapshot(snapshot),
        tradable=set(FUNDS),
        as_of=AS_OF,
        provider=_Hold(),
        prompt=lied,
        prints=prints,
        research_snapshot=snapshot,
    )
    stored = json.loads((tmp_path / "gate.jsonl").read_text(encoding="utf-8").splitlines()[-2])
    assert stored["research_snapshot"]["portfolio"]["holdings"][0]["price_gbp"] == snapshot["portfolio"]["holdings"][0]["price_gbp"]
    assert result["status"] == "decided"
    assert canonical_snapshot_sha256(snapshot) == project_snapshot(snapshot)["canonical_snapshot_sha256"]


def test_market_snapshot_nav_includes_the_receivable() -> None:
    book = Book()
    book.cash = Decimal("5000.00")
    book.quantities["VWRP"] = Decimal("40")
    declare_distribution(book, "VWRP", Decimal("50.00"), date(2026, 11, 20))
    moment = datetime(2026, 11, 11, 16, 30, tzinfo=UTC)
    prints = [(name, date(2026, 11, 11), Decimal("100"), moment) for name in FUNDS]
    snapshot = build_invest_snapshot(book=book, prints=prints, as_of=moment, requested_tradable=set(FUNDS))
    market = _market_snapshot(book, prices_from_snapshot(snapshot), snapshot, moment)
    assert market is not None
    assert snapshot["portfolio"]["nav_gbp"] == "9050.00"
    assert format(market.nav_gbp, "f") == "9050.00"
    assert snapshot["portfolio"]["spendable_cash_gbp"] == "5000.00"
    assert snapshot["portfolio"]["cash_gbp"] == "5000.00"
    assert snapshot["portfolio"]["receivable_gbp"] == "50.00"


def test_yahoo_gbp_drops_float_residue() -> None:
    assert normalise_yahoo_gbp(Decimal("143.16000366210938")) == Decimal("143.1600")
    assert normalise_yahoo_gbp(Decimal("9.642499923706055")) == Decimal("9.6425")
    assert normalise_yahoo_gbp(Decimal("4.534999847412109")) == Decimal("4.5350")


def _assumed(ticker: str, session: date, price: Decimal = Decimal("100")) -> PricePrint:
    return PricePrint(
        ticker=ticker,
        session=session,
        price=price,
        available_at=assumed_available_at(session),
        exchange_close_at=exchange_close_at(session),
        provider_published_at=None,
        retrieved_at=datetime(2026, 11, 13, 18, 0, tzinfo=UTC),
        availability_basis="assumed-next-session",
        source="yahoo-chart",
    )


def _holding(snapshot: dict[str, object], ticker: str) -> dict[str, object]:
    portfolio = snapshot["portfolio"]
    assert isinstance(portfolio, dict)
    holdings = portfolio["holdings"]
    assert isinstance(holdings, list)
    return next(row for row in holdings if isinstance(row, dict) and row["instrument_id"] == ticker)


def json_text(snapshot: dict[str, object]) -> str:
    import json

    return json.dumps(snapshot)


class _Silent:
    def post(self, **_kwargs: object) -> object:
        raise AssertionError("network")


class _Hold:
    def complete(self, snapshot: object, *, prompt: str) -> object:
        del prompt
        from sterling.research.grok.providers import ProviderEnvelope

        as_of = snapshot.as_of  # type: ignore[attr-defined]
        weights = {name: "0" for name in FUNDS}
        weights[CASH] = "1"
        return ProviderEnvelope(model_id="mock-grok", responded_at=as_of, raw_response=allocation_json(weights, decision="HOLD"))

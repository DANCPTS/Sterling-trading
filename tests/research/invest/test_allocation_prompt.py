"""The weekly prompt carries the book and only prices known at the decision."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.grok.providers import MockGrokProvider
from sterling.research.grok.schema import MarketSnapshot, estimate_tokens
from sterling.research.invest.adapter import LiveAllocationAdapter
from sterling.research.invest.book import Book
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.prompt import allocation_prompt
from sterling.research.invest.schema import CASH, FUNDS, allocation_json
from sterling.xai_client import UsageRecord

AS_OF = datetime(2026, 11, 11, 16, 30, tzinfo=UTC)


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr("urllib.request.urlopen", _blocked)


def test_prompt_is_point_in_time_and_shared(tmp_path: Path, stamped: None) -> None:
    del stamped
    book = Book()
    book.quantities["VWRP"] = Decimal("10")
    book.cash = Decimal("5000.00")
    prints = _prints()
    prints.append(("VWRP", date(2026, 11, 12), Decimal("999"), AS_OF + timedelta(days=1)))
    first = allocation_prompt(book=book, prints=prints, as_of=AS_OF, tradable=set(FUNDS))
    second = allocation_prompt(book=book, prints=prints, as_of=AS_OF, tradable=set(FUNDS))
    assert first == second
    assert "999" not in first
    assert "2026-11-12" not in first
    assert "VWRP" in first and "5000.00" in first and '"units":"10"' in first
    assert "staleness" in first and "stale" in first
    assert "prompt_hash" not in first and "response_hash" not in first
    assert all(name in first for name in (*FUNDS, CASH))
    estimate = estimate_tokens(first)
    assert estimate < 8000

    captured: list[str] = []

    class _RecordingMock:
        def complete(self, snapshot: MarketSnapshot, *, prompt: str):
            captured.append(prompt)
            return MockGrokProvider(seed=1, response_format="allocation").complete(snapshot, prompt=prompt)

    fake = _FakeClient()
    live = LiveAllocationAdapter(fake)
    snapshot = _snapshot()
    mock_envelope = _RecordingMock().complete(snapshot, prompt=first)
    live_envelope = live.complete(snapshot, prompt=first)
    assert captured == [first]
    assert fake.prompts == [first]
    assert fake.schemas[0]["required"] == [
        "model_id",
        "confidence",
        "decision",
        "weights",
        "reasons",
        "portfolio_reasoning",
        "identified_risks",
        "change_justification",
    ]
    assert "prompt_hash" not in json_schema_text(fake.schemas[0])
    assert mock_envelope.raw_response
    assert live_envelope.raw_response.startswith("{")
    assert live.last_usage is not None
    assert live.last_usage["input_tokens"] == 1500
    assert live.last_usage["cached_tokens"] == 20
    assert live.last_usage["output_tokens"] == 90
    assert live.last_usage["reasoning_tokens"] == 400
    assert live.last_usage["cost_in_usd_ticks"] == 130_000_000
    assert live.last_usage["provider_request_id"] == "req-fixture-1"
    assert live.last_usage["latency_ms"] == 850

    log = tmp_path / "mock.jsonl"
    result = run_invest_week(
        log_path=log,
        book=Book(),
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=AS_OF,
        provider=live,
        prompt=first,
        mode="simulated_clock",
    )
    assert result["provider_calls"] == 1
    text = log.read_text(encoding="utf-8")
    assert '"prompt_sha256"' in text
    assert '"record_type":"model_usage"' in text
    assert "req-fixture-1" in text
    assert "130000000" in text
    assert fake.calls == 2


def json_schema_text(schema: object) -> str:
    import json

    return json.dumps(schema)


class _FakeClient:
    model = "grok-4.3"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.schemas: list[object] = []
        self.calls = 0

    def complete(self, prompt: str, *, schema: object = None) -> tuple[str, UsageRecord]:
        self.calls += 1
        self.prompts.append(prompt)
        self.schemas.append(schema)
        weights = {name: format(Decimal(1) / Decimal(7), "f") for name in (*FUNDS, CASH)}
        usage = UsageRecord(
            request_hash="ab" * 32,
            model=self.model,
            input_tokens=1500,
            output_tokens=90,
            reasoning_tokens=400,
            cached_tokens=20,
            cost_in_usd_ticks=130_000_000,
            latency_ms=850,
            http_status=200,
            provider_request_id="req-fixture-1",
        )
        return allocation_json(weights, model_id=self.model), usage


def _prints() -> list[tuple[str, date, Decimal, datetime]]:
    fresh = AS_OF
    old = datetime(2026, 11, 2, 16, 30, tzinfo=UTC)
    rows = []
    for name in FUNDS:
        when = old if name == "IGLT" else fresh
        session = date(2026, 11, 2) if name == "IGLT" else date(2026, 11, 11)
        rows.append((name, session, Decimal("100"), when))
    return rows


def _snapshot() -> MarketSnapshot:
    from sterling.research.grok.schema import InstrumentQuote

    return MarketSnapshot(
        as_of=AS_OF,
        quotes=tuple(InstrumentQuote(name, Decimal("100"), AS_OF, "fixture") for name in FUNDS),
        positions=(),
        nav_gbp=Decimal("10000"),
        universe=FUNDS,
    )

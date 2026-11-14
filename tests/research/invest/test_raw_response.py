"""A live reply is stored raw, without the key, and can be parsed again."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.invest.adapter import LiveAllocationAdapter
from sterling.research.invest.book import Book
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.schema import CASH, FUNDS, Allocation, allocation_json, parse_allocation
from sterling.xai_client import RETRIES_AFTER_SEND, HttpResponse, XaiClient

AS_OF = datetime(2026, 11, 12, 16, 30, tzinfo=UTC)
KEY = "super-secret-xai-key"


class _Transport:
    def __init__(self, responses: list[HttpResponse] | None = None, error: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.posts = 0

    def post(self, **_kwargs: object) -> HttpResponse:
        self.posts += 1
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")
    monkeypatch.setenv("XAI_API_KEY", KEY)


def _log(tmp_path: Path) -> Path:
    return tmp_path / "evidence" / "invest-v2" / "mock" / "forward.jsonl"


def _prints() -> list[tuple[str, date, Decimal, datetime]]:
    return [(name, AS_OF.date(), Decimal("100"), AS_OF) for name in FUNDS]


def _weights() -> dict[str, str]:
    return {
        "VWRP": "0.25",
        "CUKX": "0.15",
        "IGLT": "0.10",
        "AGBP": "0.00",
        "SGLN": "0.00",
        "XSTR": "0.00",
        CASH: "0.50",
    }


def _http(content: str) -> bytes:
    body = {
        "choices": [{"message": {"content": content, "role": "assistant"}}],
        "id": "req-raw-1",
        "model": "grok-4.3",
        "note": f"Authorization: Bearer {KEY}",
        "usage": {"completion_tokens": 20, "prompt_tokens": 30},
    }
    return json.dumps(body).encode("utf-8")


def test_stored_raw_body_reparses_and_hides_the_key(stamped: None, tmp_path: Path) -> None:
    del stamped
    assert RETRIES_AFTER_SEND == 0
    content = allocation_json(
        _weights(),
        model_id="grok-4.3",
        decision="ALLOCATE",
        portfolio_reasoning="Stored raw reply.",
        change_justification="Audit the stored body.",
        identified_risks=("recorded secret must stay out of the log",),
    )
    transport = _Transport([HttpResponse(status=200, body=_http(content))])
    client = XaiClient(enabled=True, transport=transport, sleeper=lambda _delay: None)
    result = run_invest_week(
        log_path=_log(tmp_path),
        book=Book(),
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=AS_OF,
        provider=LiveAllocationAdapter(client),
        prompt="raw-response audit",
        mode="simulated_clock",
        prints=_prints(),
    )
    assert result["gate"] == "accepted"
    assert transport.posts == 1
    text = _log(tmp_path).read_text(encoding="utf-8")
    assert KEY not in text
    assert "Authorization" not in text
    assert "Bearer" not in text
    rows = [json.loads(line) for line in text.splitlines()]
    decision = next(row for row in rows if row["record_type"] == "decision")
    usage = next(row for row in rows if row["record_type"] == "model_usage")
    raw_reply = decision["raw_reply"]
    assert isinstance(raw_reply, str)
    assert decision["raw_response_sha256"] == hashlib.sha256(raw_reply.encode("utf-8")).hexdigest()
    assert usage["raw_response_sha256"] == decision["raw_response_sha256"]
    assert usage["assistant_content"] == raw_reply
    assert usage["http_body_sha256"] == hashlib.sha256(str(usage["http_body"]).encode("utf-8")).hexdigest()
    restored = json.loads(str(usage["http_body"]))["choices"][0]["message"]["content"]
    assert restored == raw_reply
    parsed = parse_allocation(raw_reply, expected_model_id="grok-4.3")
    assert isinstance(parsed, Allocation)
    weights = decision["accepted_weights"]
    assert isinstance(weights, dict)
    assert parsed.decision == decision["decision"]
    assert format(parsed.weights["VWRP"], "f") == weights["VWRP"]
    assert ProposalLog(_log(tmp_path)).verify() == len(rows)


def test_connection_drop_after_send_is_not_retried(stamped: None, tmp_path: Path) -> None:
    del stamped
    transport = _Transport(error=ConnectionResetError(f"reset {KEY}"))
    client = XaiClient(enabled=True, transport=transport, max_attempts=3, sleeper=lambda _delay: None)
    result = run_invest_week(
        log_path=_log(tmp_path),
        book=Book(),
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=AS_OF,
        provider=LiveAllocationAdapter(client),
        prompt="drop",
        mode="simulated_clock",
        prints=_prints(),
    )
    assert transport.posts == 1
    assert result["status"] == "failed"
    assert result["sent_state"] == "possibly_sent_or_billed"
    assert result["provider_calls"] == 1
    text = _log(tmp_path).read_text(encoding="utf-8")
    assert KEY not in text
    failure = next(json.loads(line) for line in text.splitlines() if '"provider_failure"' in line)
    assert failure["sent_state"] == "possibly_sent_or_billed"
    assert failure["kind"] == "XaiError"

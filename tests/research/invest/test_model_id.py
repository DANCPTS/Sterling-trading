"""model_id is the configured client id. A wrong id is kept and rejected."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.grok.providers import ProviderEnvelope
from sterling.research.invest.book import Book
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.schema import (
    CASH,
    FUNDS,
    Allocation,
    AllocationFailure,
    allocation_json,
    allocation_schema,
    parse_allocation,
)
from sterling.xai_client import DEFAULT_MODEL

AS_OF = datetime(2026, 11, 12, 16, 30, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "first_paid_raw_reply.txt"
EVIDENCE_COPY = Path("/cursor/stores/self/first_paid_research_run/raw_response.txt")


def _historical_reply() -> str:
    """Exact assistant text. The paid-call bundle is only compared, never written."""
    text = FIXTURE.read_text(encoding="utf-8")
    if EVIDENCE_COPY.is_file():
        assert text == EVIDENCE_COPY.read_text(encoding="utf-8")
    return text


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")


def _hold(model_id: str) -> str:
    weights = {name: "0" for name in FUNDS}
    weights[CASH] = "1"
    return allocation_json(
        weights,
        model_id=model_id,
        decision="HOLD",
        portfolio_reasoning="Stay in cash.",
        change_justification="No fund is added.",
        identified_risks=("cash is the current book",),
    )


class _Reply:
    def __init__(self, raw: str, *, reported: str | None = None) -> None:
        self._raw = raw
        self._reported = reported

    def complete(self, snapshot: object, *, prompt: str) -> ProviderEnvelope:
        del prompt
        as_of = snapshot.as_of  # type: ignore[attr-defined]
        return ProviderEnvelope(
            model_id=DEFAULT_MODEL,
            responded_at=as_of,
            raw_response=self._raw,
            provider_reported_model=self._reported,
        )


def _run(tmp_path: Path, name: str, raw: str, *, reported: str | None = None) -> dict[str, object]:
    log = tmp_path / name / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    run_invest_week(
        log_path=log,
        book=Book(),
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=AS_OF,
        provider=_Reply(raw, reported=reported),
        prompt="model id",
        mode="simulated_clock",
    )
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    decision = next(row for row in rows if row["record_type"] == "decision")
    failure = next((row for row in rows if row["record_type"] == "provider_failure"), None)
    return {"decision": decision, "failure": failure, "log": log}


def test_schema_enum_comes_from_the_configured_model() -> None:
    schema = allocation_schema(DEFAULT_MODEL)
    assert schema["properties"]["model_id"] == {"enum": [DEFAULT_MODEL], "type": "string"}  # type: ignore[index]


def test_correct_hold_is_explicit(stamped: None, tmp_path: Path) -> None:
    del stamped
    result = _run(tmp_path, "ok", _hold(DEFAULT_MODEL), reported=DEFAULT_MODEL)
    decision = result["decision"]
    assert isinstance(decision, dict)
    assert decision["gate"] == "hold"
    assert decision["reasons"] == ["explicit_hold"]
    assert decision["decision"] == "HOLD"
    assert decision["json_model_id"] == DEFAULT_MODEL
    assert decision["raw_reply"] == _hold(DEFAULT_MODEL)
    assert result["failure"] is None


def test_incorrect_and_missing_ids_are_rejected(stamped: None, tmp_path: Path) -> None:
    del stamped
    wrong = _run(tmp_path, "wrong", _hold("paper-v2-allocation"))
    decision = wrong["decision"]
    assert isinstance(decision, dict)
    assert decision["reasons"] == ["model_id_mismatch"]
    assert decision["disposition"] == "HOLD"
    assert "paper-v2-allocation" in str(decision["raw_reply"])
    assert decision.get("json_model_id") is None
    failure = wrong["failure"]
    assert isinstance(failure, dict)
    assert failure["message"] == "model_id_mismatch"
    missing = parse_allocation(_hold(DEFAULT_MODEL).replace('"model_id":"grok-4.3",', ""))
    assert isinstance(missing, AllocationFailure)
    assert missing.reason == "malformed_json"


def test_provider_reported_model_disagreement_is_rejected(stamped: None, tmp_path: Path) -> None:
    del stamped
    result = _run(tmp_path, "http", _hold(DEFAULT_MODEL), reported="grok-other")
    decision = result["decision"]
    assert isinstance(decision, dict)
    assert decision["json_model_id"] == DEFAULT_MODEL
    assert "provider_model_mismatch" in decision["reasons"]
    assert decision["provider_reported_model"] == "grok-other"
    assert decision["disposition"] == "HOLD"
    failure = result["failure"]
    assert isinstance(failure, dict)
    assert failure["kind"] == "model_identity"


def test_historical_first_paid_reply_is_still_rejected(stamped: None, tmp_path: Path) -> None:
    del stamped
    historical = _historical_reply()
    parsed = parse_allocation(historical, expected_model_id=DEFAULT_MODEL)
    assert isinstance(parsed, AllocationFailure)
    assert parsed.reason == "model_id_mismatch"
    result = _run(tmp_path, "historical", historical, reported=DEFAULT_MODEL)
    decision = result["decision"]
    assert isinstance(decision, dict)
    assert decision["raw_reply"] == historical
    assert json.loads(str(decision["raw_reply"]))["model_id"] == "paper-v2-allocation"
    assert decision["reasons"] == ["model_id_mismatch"]


def test_hypothetical_corrected_id_is_an_explicit_hold() -> None:
    """A copy with only model_id changed. This is not written to the paid-call evidence."""
    body = json.loads(_historical_reply())
    body["model_id"] = DEFAULT_MODEL
    parsed = parse_allocation(json.dumps(body), expected_model_id=DEFAULT_MODEL)
    assert isinstance(parsed, Allocation)
    assert parsed.decision == "HOLD"
    assert parsed.model_id == DEFAULT_MODEL
    assert parsed.weights[CASH] == Decimal("1")
    assert all(parsed.weights[name] == 0 for name in FUNDS)

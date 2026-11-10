"""Forward log: hash chain, no backdating, and scores that cannot see the past."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.errors import ResearchError, ResearchIsolationError
from sterling.research.fx.grok_forward import (
    ForwardProposal,
    GrokApiHook,
    OfflineStubSource,
    ProposalLog,
    score_proposal,
)

T0 = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)


def _proposal(when: datetime, weights: dict[str, str] | None = None) -> ForwardProposal:
    import json

    body = json.dumps({"book": "fx", "weights": weights or {}}, sort_keys=True, separators=(",", ":"))
    return ForwardProposal(
        timestamp_utc=when,
        model="offline-stub",
        model_version="fx-forward-0",
        prompt_hash="a" * 64,
        inputs_snapshot_hash="b" * 64,
        proposal=body,
        rationale="frozen before the fix",
    )


def test_log_rejects_backdated_and_pre_creation_proposals(tmp_path: Path) -> None:
    path = tmp_path / "forward.jsonl"
    log = ProposalLog(path)
    log.create(now=T0)
    created_hash = path.read_text(encoding="utf-8").splitlines()[0]
    with pytest.raises(ResearchError, match="before the log was created"):
        log.append_proposal(_proposal(T0 - timedelta(days=30)), price_timestamps=())
    with pytest.raises(ResearchError, match="not after the previous"):
        log.append_proposal(_proposal(T0), price_timestamps=())
    later = T0 + timedelta(hours=1)
    prices = (later - timedelta(minutes=30),)
    log.append_proposal(_proposal(later), price_timestamps=prices)
    assert path.read_text(encoding="utf-8").splitlines()[0] == created_hash
    assert log.verify() == 2
    with pytest.raises(ResearchError, match="not after the previous"):
        log.append_proposal(_proposal(later - timedelta(minutes=1)), price_timestamps=())


def test_chain_detects_tampering_and_inputs_must_predate_the_proposal(tmp_path: Path) -> None:
    path = tmp_path / "forward.jsonl"
    log = ProposalLog(path)
    log.create(now=T0)
    proposal = _proposal(T0 + timedelta(hours=2))
    with pytest.raises(ResearchError, match="not strictly before"):
        log.append_proposal(proposal, price_timestamps=(proposal.timestamp_utc,))
    log.append_proposal(proposal, price_timestamps=(T0,))
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("log_created", "log_creatad")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ResearchError, match="chain broke"):
        log.verify()


def test_scorer_uses_only_prices_after_the_timestamp() -> None:
    when = T0 + timedelta(days=1)
    proposal = _proposal(when, {"USD": "1"})
    before = (when - timedelta(hours=2), Decimal(1))
    at_stamp = (when, Decimal(50))
    entry = (when + timedelta(hours=1), Decimal(2))
    exit_price = (when + timedelta(days=2), Decimal(4))
    value = score_proposal(proposal, {"USD": (before, at_stamp, entry, exit_price)})
    assert value == Decimal(1)
    unchanged = score_proposal(proposal, {"USD": ((when - timedelta(days=9), Decimal(99)), entry, exit_price)})
    assert unchanged == value
    with pytest.raises(ResearchError, match="no price"):
        score_proposal(proposal, {"USD": (before, at_stamp)})
    cash = _proposal(when)
    assert score_proposal(cash, {"USD": (entry, exit_price)}) == Decimal(0)


def test_stub_is_offline_and_the_hook_sends_nothing() -> None:
    stub = OfflineStubSource()
    earlier = ((T0 - timedelta(days=1), Decimal("1.25")),)
    proposal = stub.propose(timestamp_utc=T0, prompt="hold cash", inputs={"USD": earlier}, rationale="no model")
    assert proposal.model == "offline-stub"
    assert '"weights":{}' in proposal.proposal
    with pytest.raises(ResearchError, match="not before"):
        stub.propose(timestamp_utc=T0, prompt="hold cash", inputs={"USD": ((T0, Decimal(1)),)}, rationale="late")
    with pytest.raises(ResearchIsolationError, match="disabled"):
        GrokApiHook().complete("forecast")
    with pytest.raises(ResearchIsolationError, match="does not send"):
        GrokApiHook(owner_approved=True).complete("forecast")

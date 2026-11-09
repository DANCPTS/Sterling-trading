"""Proposal interface and the forward-only logging schema.

A rules strategy and, later, a Grok source both return a ``Proposal``. The
Grok class in this milestone is a stub: it does not call a model or the
network. Every real proposal records the data cutoff, and a cutoff after the
signal date is rejected.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sterling.research.errors import ResearchError, ResearchIsolationError
from sterling.research.strategy import MarketView, Strategy

SCHEMA = "research-proposal-v1"


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    decision_timestamp: datetime
    signal_date: date
    data_cutoff: date
    source_type: str
    strategy_id: str
    model_version: str | None
    prompt_version: str | None
    input_digest: str
    weights: tuple[tuple[str, Decimal], ...]
    raw_output: str

    def __post_init__(self) -> None:
        if self.data_cutoff > self.signal_date:
            raise ResearchError("proposal data cutoff is after the signal date")
        if self.decision_timestamp.tzinfo is None:
            raise ResearchError("proposal timestamp must be timezone-aware")

    def weight_map(self) -> dict[str, Decimal]:
        return {ticker: weight for ticker, weight in self.weights}

    def as_record(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "record_type": "proposal",
            "proposal_id": self.proposal_id,
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "signal_date": self.signal_date.isoformat(),
            "data_cutoff": self.data_cutoff.isoformat(),
            "source_type": self.source_type,
            "strategy_id": self.strategy_id,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "input_digest": self.input_digest,
            "output": {ticker: format(weight, "f") for ticker, weight in self.weights},
            "raw_output": self.raw_output,
        }


def input_digest(view: MarketView) -> str:
    lines: list[str] = [view.as_of.isoformat()]
    for ticker in sorted(view.closes):
        for session, price in view.closes[ticker]:
            lines.append(f"{ticker},{session.isoformat()},{format(price, 'f')}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _proposal_id(strategy_id: str, signal_date: date, digest: str, weights: Mapping[str, Decimal]) -> str:
    body = {
        "digest": digest,
        "signal_date": signal_date.isoformat(),
        "strategy_id": strategy_id,
        "weights": {ticker: format(weights[ticker], "f") for ticker in sorted(weights)},
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class RulesProposalSource:
    """Wrap a deterministic strategy. ``model_version`` stays null."""

    source_type = "rules"
    model_version = None
    prompt_version = None

    def __init__(self, strategy: Strategy) -> None:
        self.strategy = strategy

    def propose(
        self,
        view: MarketView,
        instrument_ids: Sequence[str],
        *,
        decision_timestamp: datetime,
    ) -> Proposal:
        weights = dict(self.strategy.target_weights(view, instrument_ids))
        digest = input_digest(view)
        ordered = tuple((ticker, weights[ticker]) for ticker in sorted(weights))
        raw = json.dumps({ticker: format(weight, "f") for ticker, weight in ordered}, sort_keys=True)
        return Proposal(
            proposal_id=_proposal_id(self.strategy.strategy_id, view.as_of, digest, weights),
            decision_timestamp=decision_timestamp,
            signal_date=view.as_of,
            data_cutoff=view.as_of,
            source_type=self.source_type,
            strategy_id=self.strategy.strategy_id,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            input_digest=digest,
            weights=ordered,
            raw_output=raw,
        )


class GrokProposalStub:
    """Interface placeholder for a later forward-only Grok source.

    Calling ``propose`` does not contact a model. A future implementation must
    log the same schema, with the input cutoff set to the last bar the prompt
    was allowed to see, and must not be wired to an order path.
    """

    source_type = "ai_stub"
    model_version = "UNCONFIGURED"
    prompt_version = "UNCONFIGURED"
    strategy_id = "grok_stub"

    def propose(
        self,
        view: MarketView,
        instrument_ids: Sequence[str],
        *,
        decision_timestamp: datetime,
    ) -> Proposal:
        del view, instrument_ids, decision_timestamp
        raise ResearchIsolationError("Grok proposal source is a logging stub. No model is called and no network is used.")

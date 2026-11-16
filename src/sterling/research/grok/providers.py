"""Grok providers. The mock is deterministic. The xAI class sends nothing.

No key is stored in the repository. Tests and the default path do not set the
config flag, so the stub refuses before it could do anything else. Even with
the flag and an environment variable both set, this build has no client and
does not open a socket.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sterling.research.errors import ResearchError, ResearchIsolationError
from sterling.research.fx.grok_forward import require_utc
from sterling.research.grok.schema import MarketSnapshot, current_weights
from sterling.research.invest.schema import CASH, FUNDS, allocation_json
from sterling.research.money import ZERO, quantize_weight_down

XAI_ENV_KEY = "XAI_API_KEY"
_WEIGHT_BAND = Decimal("0.005")


@dataclass(frozen=True, slots=True)
class ProviderEnvelope:
    model_id: str
    responded_at: datetime
    raw_response: str
    provider_reported_model: str | None = None

    def __post_init__(self) -> None:
        require_utc(self.responded_at)
        if not self.model_id.strip():
            raise ResearchError("provider model id is missing")
        if not isinstance(self.raw_response, str):
            raise ResearchError("provider response must be text")


@dataclass(frozen=True, slots=True)
class ScriptStep:
    """One scripted reply. ``responded_after`` is added to the snapshot as-of time."""

    raw_response: str
    responded_after: timedelta = timedelta(0)
    model_id: str = "mock-grok"


class MockGrokProvider:
    """Seeded provider. Scripted steps run first, then an equal-weight fallback.

    The fallback reads only the snapshot it is given. It does not see later
    bars, and it does not choose the frozen rules parameters.
    """

    def __init__(self, steps: Sequence[ScriptStep] = (), *, seed: int = 0, response_format: str = "proposals") -> None:
        if response_format not in {"proposals", "allocation"}:
            raise ResearchError("mock response format must be proposals or allocation")
        self._steps = list(steps)
        self._seed = seed
        self._response_format = response_format
        self.calls = 0

    def complete(self, snapshot: MarketSnapshot, *, prompt: str) -> ProviderEnvelope:
        del prompt
        self.calls += 1
        if self._steps:
            step = self._steps.pop(0)
            return ProviderEnvelope(
                model_id=step.model_id,
                responded_at=require_utc(snapshot.as_of + step.responded_after),
                raw_response=step.raw_response,
            )
        model_id = f"mock-grok-{self._seed}"
        raw = (
            _allocation_response(model_id)
            if self._response_format == "allocation"
            else _equal_weight_response(snapshot, model_id=model_id)
        )
        return ProviderEnvelope(model_id=model_id, responded_at=require_utc(snapshot.as_of), raw_response=raw)


class XaiGrokProvider:
    """Unwired xAI client. There is no URL, no socket, and no shipped key."""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled

    def complete(self, snapshot: MarketSnapshot, *, prompt: str) -> ProviderEnvelope:
        del snapshot, prompt
        key = os.environ.get(XAI_ENV_KEY)
        if not self.enabled or not key:
            raise ResearchIsolationError(
                "xAI provider refuses to run without an explicit config flag and the XAI_API_KEY environment variable. "
                "No request was sent."
            )
        raise ResearchIsolationError(
            "xAI provider is unwired in this build. The flag and key were present and were not used. No request was sent."
        )


def _equal_weight_response(snapshot: MarketSnapshot, *, model_id: str) -> str:
    import json

    count = Decimal(len(snapshot.universe))
    target = quantize_weight_down(Decimal(1) / count)
    held = current_weights(snapshot)
    proposals: list[dict[str, str]] = []
    for instrument_id in snapshot.universe:
        current = held.get(instrument_id, ZERO)
        if target > current + _WEIGHT_BAND:
            action = "BUY"
        elif target + _WEIGHT_BAND < current:
            action = "SELL"
        else:
            continue
        proposals.append(
            {
                "action": action,
                "confidence": "0.35",
                "instrument_id": instrument_id,
                "reasoning": "mock fallback uses only this snapshot",
                "target_weight": format(target, "f"),
            }
        )
    if not proposals:
        proposals.append(
            {
                "action": "HOLD",
                "confidence": "0.35",
                "instrument_id": snapshot.universe[0],
                "reasoning": "mock fallback holds the supplied book",
            }
        )
    return json.dumps({"model_id": model_id, "proposals": proposals}, sort_keys=True, separators=(",", ":"))


def _allocation_response(model_id: str) -> str:
    weight = format((Decimal(1) / Decimal(7)), "f")
    weights = {name: weight for name in (*FUNDS, CASH)}
    return allocation_json(weights, model_id=model_id)

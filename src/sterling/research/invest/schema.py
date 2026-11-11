"""Target allocations for paper-v2. Actions are not part of the reply."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from sterling.research.errors import ResearchError
from sterling.research.grok.preregistration import APPROVED_UNIVERSE
from sterling.research.invest.registration import SUM_TOLERANCE
from sterling.research.money import ZERO, parse_decimal

FUNDS = APPROVED_UNIVERSE
CASH = "CASH"
NAMES = (*FUNDS, CASH)
MAX_REASON = 500

ALLOCATION_SCHEMA: dict[str, object] = {
    "additionalProperties": False,
    "properties": {
        "confidence": {"type": "string"},
        "model_id": {"type": "string"},
        "reasons": {
            "additionalProperties": False,
            "properties": {name: {"type": "string"} for name in FUNDS},
            "required": list(FUNDS),
            "type": "object",
        },
        "weights": {
            "additionalProperties": False,
            "properties": {name: {"type": "string"} for name in NAMES},
            "required": list(NAMES),
            "type": "object",
        },
    },
    "required": ["model_id", "confidence", "weights", "reasons"],
    "type": "object",
}


@dataclass(frozen=True, slots=True)
class Allocation:
    model_id: str
    confidence: Decimal
    weights: dict[str, Decimal]
    reasons: dict[str, str]


@dataclass(frozen=True, slots=True)
class AllocationFailure:
    reason: str


def parse_allocation(raw: str) -> Allocation | AllocationFailure:
    """Accept six fund weights plus cash. Normalise only inside the tolerance."""
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return AllocationFailure("malformed_json")
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return AllocationFailure("malformed_json")
    if not isinstance(loaded, dict):
        return AllocationFailure("malformed_json")
    extra = set(loaded) - {"model_id", "confidence", "weights", "reasons"}
    if extra:
        return AllocationFailure("unknown_field")
    model_id = loaded.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 80:
        return AllocationFailure("malformed_json")
    confidence = _unit(loaded.get("confidence"))
    if confidence is None:
        return AllocationFailure("confidence_out_of_range")
    weights = _weights(loaded.get("weights"))
    if isinstance(weights, AllocationFailure):
        return weights
    reasons = _reasons(loaded.get("reasons"))
    if isinstance(reasons, AllocationFailure):
        return reasons
    return Allocation(model_id=model_id, confidence=confidence, weights=weights, reasons=reasons)


def allocation_json(
    weights: dict[str, str],
    *,
    reasons: dict[str, str] | None = None,
    confidence: str = "0.50",
    model_id: str = "mock-grok",
) -> str:
    text = reasons or {name: "mock sleeve" for name in FUNDS}
    body = {"confidence": confidence, "model_id": model_id, "reasons": text, "weights": weights}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _unit(value: object) -> Decimal | None:
    try:
        amount = parse_decimal(value if isinstance(value, str) else "no", label="confidence")
    except ResearchError:
        return None
    if amount < 0 or amount > 1:
        return None
    return amount


def _weights(value: object) -> dict[str, Decimal] | AllocationFailure:
    if not isinstance(value, dict):
        return AllocationFailure("malformed_json")
    if set(value) != set(NAMES):
        return AllocationFailure("unknown_field" if set(value) - set(NAMES) else "missing_weight")
    parsed: dict[str, Decimal] = {}
    for name in NAMES:
        raw = value[name]
        try:
            amount = parse_decimal(raw if isinstance(raw, str) else "no", label=name)
        except ResearchError:
            return AllocationFailure("malformed_json")
        if amount < 0 or amount > 1:
            return AllocationFailure("weight_out_of_range")
        parsed[name] = amount
    total = sum(parsed.values(), ZERO)
    if total <= 0 or abs(total - 1) > SUM_TOLERANCE:
        return AllocationFailure("weight_sum")
    factor = Decimal(1) / total
    normalised = {name: parsed[name] * factor for name in FUNDS}
    normalised[CASH] = Decimal(1) - sum(normalised.values(), ZERO)
    if normalised[CASH] < 0 or normalised[CASH] > 1:
        return AllocationFailure("weight_sum")
    return normalised


def _reasons(value: object) -> dict[str, str] | AllocationFailure:
    if not isinstance(value, dict) or set(value) != set(FUNDS):
        return AllocationFailure("malformed_json")
    reasons: dict[str, str] = {}
    for name in FUNDS:
        text = value[name]
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_REASON or "\x00" in text:
            return AllocationFailure("malformed_json")
        reasons[name] = text.strip()
    return reasons

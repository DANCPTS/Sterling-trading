"""Target allocations for paper-v2. Actions are not part of the reply."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from sterling.research.errors import ResearchError
from sterling.research.grok.preregistration import APPROVED_UNIVERSE
from sterling.research.money import ZERO, parse_decimal

FUNDS = APPROVED_UNIVERSE
CASH = "CASH"
NAMES = (*FUNDS, CASH)
MAX_REASON = 500
MAX_PORTFOLIO_REASON = 2000
MAX_JUSTIFICATION = 1000
MAX_RISKS = 8
DECISIONS = ("ALLOCATE", "HOLD", "NO_TRADE")
_FIELDS = {
    "model_id",
    "confidence",
    "decision",
    "weights",
    "reasons",
    "portfolio_reasoning",
    "identified_risks",
    "change_justification",
}

ALLOCATION_SCHEMA: dict[str, object] = {
    "additionalProperties": False,
    "properties": {
        "change_justification": {"type": "string"},
        "confidence": {"type": "string"},
        "decision": {"enum": list(DECISIONS), "type": "string"},
        "identified_risks": {"items": {"type": "string"}, "maxItems": MAX_RISKS, "minItems": 1, "type": "array"},
        "model_id": {"type": "string"},
        "portfolio_reasoning": {"type": "string"},
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
    "required": [
        "model_id",
        "confidence",
        "decision",
        "weights",
        "reasons",
        "portfolio_reasoning",
        "identified_risks",
        "change_justification",
    ],
    "type": "object",
}


@dataclass(frozen=True, slots=True)
class Allocation:
    model_id: str
    confidence: Decimal
    weights: dict[str, Decimal]
    reasons: dict[str, str]
    decision: str
    portfolio_reasoning: str
    identified_risks: tuple[str, ...]
    change_justification: str


@dataclass(frozen=True, slots=True)
class AllocationFailure:
    reason: str


def parse_allocation(raw: str, *, expected_model_id: str | None = None) -> Allocation | AllocationFailure:
    """Accept one allocation. ``expected_model_id`` is the configured client, not the JSON claim."""
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return AllocationFailure("malformed_json")
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return AllocationFailure("malformed_json")
    if not isinstance(loaded, dict):
        return AllocationFailure("malformed_json")
    extra = set(loaded) - _FIELDS
    if extra or _FIELDS - set(loaded):
        return AllocationFailure("unknown_field" if extra else "malformed_json")
    model_id = loaded.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 80 or "\x00" in model_id:
        return AllocationFailure("malformed_json")
    model_id = model_id.strip()
    if expected_model_id is not None and model_id != expected_model_id:
        return AllocationFailure("model_id_mismatch")
    decision = loaded.get("decision")
    if decision not in DECISIONS:
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
    portfolio = _text(loaded.get("portfolio_reasoning"), MAX_PORTFOLIO_REASON)
    justification = _text(loaded.get("change_justification"), MAX_JUSTIFICATION)
    risks = _risks(loaded.get("identified_risks"))
    if portfolio is None or justification is None or isinstance(risks, AllocationFailure):
        return AllocationFailure("malformed_json")
    return Allocation(
        model_id=model_id,
        confidence=confidence,
        weights=weights,
        reasons=reasons,
        decision=str(decision),
        portfolio_reasoning=portfolio,
        identified_risks=risks,
        change_justification=justification,
    )


def allocation_json(
    weights: dict[str, str],
    *,
    reasons: dict[str, str] | None = None,
    confidence: str = "0.50",
    model_id: str = "mock-grok",
    decision: str = "ALLOCATE",
    portfolio_reasoning: str = "mock portfolio sleeve",
    identified_risks: tuple[str, ...] = ("mock allocation is not a forecast",),
    change_justification: str = "mock rebalance",
) -> str:
    text = reasons or {name: "mock sleeve" for name in FUNDS}
    body = {
        "change_justification": change_justification,
        "confidence": confidence,
        "decision": decision,
        "identified_risks": list(identified_risks),
        "model_id": model_id,
        "portfolio_reasoning": portfolio_reasoning,
        "reasons": text,
        "weights": weights,
    }
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
    from sterling.research.invest.registration import SUM_TOLERANCE

    total = sum(parsed.values(), ZERO)
    if total <= 0 or abs(total - 1) > SUM_TOLERANCE:
        return AllocationFailure("weight_sum")
    factor = Decimal(1) / total
    normalised = {name: parsed[name] * factor for name in FUNDS}
    normalised[CASH] = Decimal(1) - sum(normalised.values(), ZERO)
    if normalised[CASH] < 0 or normalised[CASH] > 1:
        return AllocationFailure("weight_sum")
    return normalised


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        return None
    return value.strip()


def _risks(value: object) -> tuple[str, ...] | AllocationFailure:
    if not isinstance(value, list) or not value or len(value) > MAX_RISKS:
        return AllocationFailure("malformed_json")
    risks: list[str] = []
    for item in value:
        text = _text(item, MAX_REASON)
        if text is None:
            return AllocationFailure("malformed_json")
        risks.append(text)
    return tuple(risks)


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

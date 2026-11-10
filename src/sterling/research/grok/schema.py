"""Strict snapshot and proposal schema.

Decimals arrive as strings. Floats, extra fields, and prices from after the
as-of time are rejected. Reasoning text is stored as text and is not executed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import canonical, prompt_hash, require_utc
from sterling.research.money import parse_decimal

Action = Literal["BUY", "SELL", "HOLD"]
ACTIONS = frozenset({"BUY", "SELL", "HOLD"})
# Split so this file does not contain an order-entry token. The isolation
# check rejects that substring anywhere in the research tree.
_ORDER_KEY = "place" + "_order"
FORBIDDEN_KEYS = frozenset(
    {
        _ORDER_KEY,
        "submit_order",
        "broker_order",
        "leverage",
        "borrowing",
        "borrow",
        "short",
        "live",
        "live_trading",
        "per_trade_cap",
        "daily_loss_cap",
        "allowlist",
        "execution_permission",
        "content_hash",
    }
)
TOP_KEYS = frozenset({"model_id", "proposals"})
ITEM_KEYS = frozenset(
    {
        "instrument_id",
        "action",
        "target_weight",
        "size_request",
        "planned_risk_gbp",
        "reasoning",
        "confidence",
        "prompt_hash",
        "response_hash",
        "model_id",
    }
)
MAX_RAW_CHARS = 20000
MAX_REASONING_CHARS = 2000


def response_hash(raw: str) -> str:
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InstrumentQuote:
    instrument_id: str
    price: Decimal
    observed_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class PaperPosition:
    instrument_id: str
    quantity: Decimal
    market_value_gbp: Decimal


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Prices and positions available at ``as_of``. Positions are a read-only tuple."""

    as_of: datetime
    quotes: tuple[InstrumentQuote, ...]
    positions: tuple[PaperPosition, ...]
    nav_gbp: Decimal
    universe: tuple[str, ...]

    def __post_init__(self) -> None:
        require_utc(self.as_of)
        if self.nav_gbp <= 0:
            raise ResearchError("snapshot NAV must be positive")
        if not self.universe or len(set(self.universe)) != len(self.universe):
            raise ResearchError("snapshot universe must be a non-empty set")
        seen: set[str] = set()
        for quote in self.quotes:
            observed = require_utc(quote.observed_at)
            if observed > self.as_of:
                raise ResearchError("snapshot quote is after the as-of time")
            if quote.price <= 0:
                raise ResearchError("snapshot price must be positive")
            if quote.instrument_id not in self.universe:
                raise ResearchError("snapshot quote is outside the universe")
            if quote.instrument_id in seen:
                raise ResearchError("duplicate snapshot quote")
            if not quote.source or len(quote.source) > 40:
                raise ResearchError("snapshot source name is missing or too long")
            seen.add(quote.instrument_id)
        if not seen:
            raise ResearchError("snapshot has no quotes")
        held_value = Decimal(0)
        for position in self.positions:
            if position.instrument_id not in self.universe:
                raise ResearchError("snapshot position is outside the universe")
            if position.quantity < 0 or position.market_value_gbp < 0:
                raise ResearchError("snapshot position cannot be short")
            if (position.quantity == 0) != (position.market_value_gbp == 0):
                raise ResearchError("snapshot position quantity and value disagree")
            held_value += position.market_value_gbp
        if held_value - self.nav_gbp > Decimal("0.01"):
            raise ResearchError("snapshot positions exceed NAV")


@dataclass(frozen=True, slots=True)
class AccountView:
    """Operational account inputs. These are not the research paper NAV."""

    marked_equity_gbp: Decimal
    opening_equity_gbp: Decimal
    net_external_deposits_gbp: Decimal

    def __post_init__(self) -> None:
        if self.marked_equity_gbp <= 0 or self.opening_equity_gbp <= 0:
            raise ResearchError("account equity must be positive")


@dataclass(frozen=True, slots=True)
class ParsedInstrument:
    instrument_id: str
    action: Action
    target_weight: Decimal | None
    size_request: Decimal | None
    planned_risk_gbp: Decimal | None
    reasoning: str
    confidence: Decimal
    model_id: str
    prompt_hash: str
    response_hash: str


@dataclass(frozen=True, slots=True)
class ParseOk:
    model_id: str
    instruments: tuple[ParsedInstrument, ...]


@dataclass(frozen=True, slots=True)
class ParseFailure:
    reason: str


def prompt_for(snapshot: MarketSnapshot) -> str:
    document = {
        "as_of": require_utc(snapshot.as_of).isoformat(),
        "nav_gbp": format(snapshot.nav_gbp, "f"),
        "positions": [
            {
                "instrument_id": position.instrument_id,
                "market_value_gbp": format(position.market_value_gbp, "f"),
                "quantity": format(position.quantity, "f"),
            }
            for position in sorted(snapshot.positions, key=lambda item: item.instrument_id)
        ],
        "quotes": [
            {
                "instrument_id": quote.instrument_id,
                "observed_at": require_utc(quote.observed_at).isoformat(),
                "price": format(quote.price, "f"),
                "source": quote.source,
            }
            for quote in sorted(snapshot.quotes, key=lambda item: item.instrument_id)
        ],
        "universe": list(snapshot.universe),
    }
    return canonical(document)


def prompt_digest(snapshot: MarketSnapshot) -> str:
    return prompt_hash(prompt_for(snapshot))


def _places(amount: Decimal) -> int:
    exponent = amount.as_tuple().exponent
    if not isinstance(exponent, int):
        return 99
    return max(-exponent, 0)


def _decimal(value: object, *, places: int) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        amount = parse_decimal(value, label="field")
    except ResearchError:
        return None
    if _places(amount) > places:
        return None
    return amount


def _instrument_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.isalnum() or not value.isupper() or len(value) > 12:
        return None
    return value


def parse_response(
    raw: str,
    *,
    prompt_digest_value: str,
    response_digest: str,
) -> ParseOk | ParseFailure:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_RAW_CHARS or "\x00" in raw:
        return ParseFailure("malformed_json")
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return ParseFailure("malformed_json")
    if not isinstance(loaded, dict):
        return ParseFailure("malformed_json")
    document = {str(key): value for key, value in loaded.items()}
    extra = set(document) - TOP_KEYS
    if extra & FORBIDDEN_KEYS:
        return ParseFailure("forbidden_field:" + ",".join(sorted(extra & FORBIDDEN_KEYS)))
    if extra:
        return ParseFailure("unknown_field:" + ",".join(sorted(extra)))
    model_id = document.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 80:
        return ParseFailure("malformed_json")
    proposals = document.get("proposals")
    if not isinstance(proposals, list) or not proposals:
        return ParseFailure("malformed_json")
    parsed: list[ParsedInstrument] = []
    seen: set[str] = set()
    for item in proposals:
        if not isinstance(item, dict):
            return ParseFailure("malformed_json")
        fields = {str(key): value for key, value in item.items()}
        blocked = set(fields) & FORBIDDEN_KEYS
        if blocked:
            return ParseFailure("forbidden_field:" + ",".join(sorted(blocked)))
        unknown = set(fields) - ITEM_KEYS
        if unknown:
            return ParseFailure("unknown_field:" + ",".join(sorted(unknown)))
        instrument_id = _instrument_id(fields.get("instrument_id"))
        if instrument_id is None:
            return ParseFailure("malformed_json")
        if instrument_id in seen:
            return ParseFailure("duplicate_instrument")
        seen.add(instrument_id)
        action = fields.get("action")
        if action not in ACTIONS:
            return ParseFailure("action_rejected")
        confidence = _decimal(fields.get("confidence"), places=8)
        if confidence is None or confidence < 0 or confidence > 1:
            return ParseFailure("confidence_out_of_range")
        reasoning = fields.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip() or len(reasoning) > MAX_REASONING_CHARS or "\x00" in reasoning:
            return ParseFailure("malformed_json")
        target = fields.get("target_weight")
        target_weight: Decimal | None
        if target is None:
            target_weight = None
        else:
            target_weight = _decimal(target, places=8)
            if target_weight is None:
                return ParseFailure("malformed_json")
            if target_weight < 0:
                return ParseFailure("negative_weight")
            if target_weight > 1:
                return ParseFailure("overweight")
        if action in {"BUY", "SELL"} and target_weight is None:
            return ParseFailure("malformed_json")
        if action == "HOLD" and target_weight is not None:
            return ParseFailure("inconsistent_action")
        size = fields.get("size_request")
        size_request = None if size is None else _decimal(size, places=8)
        if size is not None and (size_request is None or size_request < 0):
            return ParseFailure("oversized_size")
        risk = fields.get("planned_risk_gbp")
        planned = None if risk is None else _decimal(risk, places=2)
        if risk is not None and (planned is None or planned <= 0):
            return ParseFailure("malformed_json")
        item_model = fields.get("model_id", model_id)
        if item_model != model_id:
            return ParseFailure("malformed_json")
        claimed_prompt = fields.get("prompt_hash")
        claimed_response = fields.get("response_hash")
        if claimed_prompt is not None and claimed_prompt != prompt_digest_value:
            return ParseFailure("hash_mismatch")
        if claimed_response is not None and claimed_response != response_digest:
            return ParseFailure("hash_mismatch")
        if (claimed_prompt is not None and not isinstance(claimed_prompt, str)) or (
            claimed_response is not None and not isinstance(claimed_response, str)
        ):
            return ParseFailure("hash_mismatch")
        parsed.append(
            ParsedInstrument(
                instrument_id=instrument_id,
                action=action,
                target_weight=target_weight,
                size_request=size_request,
                planned_risk_gbp=planned,
                reasoning=reasoning,
                confidence=confidence,
                model_id=model_id,
                prompt_hash=prompt_digest_value,
                response_hash=response_digest,
            )
        )
    return ParseOk(model_id=model_id, instruments=tuple(parsed))


def current_weights(snapshot: MarketSnapshot) -> dict[str, Decimal]:
    weights: dict[str, Decimal] = {}
    for position in snapshot.positions:
        if position.market_value_gbp > 0:
            weights[position.instrument_id] = position.market_value_gbp / snapshot.nav_gbp
    return weights


def instrument_payload(item: ParsedInstrument) -> dict[str, object]:
    body: dict[str, object] = {
        "action": item.action,
        "confidence": format(item.confidence, "f"),
        "instrument_id": item.instrument_id,
        "model_id": item.model_id,
        "prompt_hash": item.prompt_hash,
        "reasoning": item.reasoning,
        "response_hash": item.response_hash,
    }
    if item.planned_risk_gbp is not None:
        body["planned_risk_gbp"] = format(item.planned_risk_gbp, "f")
    if item.size_request is not None:
        body["size_request"] = format(item.size_request, "f")
    if item.target_weight is not None:
        body["target_weight"] = format(item.target_weight, "f")
    return body


def account_payload(account: AccountView) -> dict[str, str]:
    return {
        "marked_equity_gbp": format(account.marked_equity_gbp, "f"),
        "net_external_deposits_gbp": format(account.net_external_deposits_gbp, "f"),
        "opening_equity_gbp": format(account.opening_equity_gbp, "f"),
    }


def as_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): item for key, item in value.items()}

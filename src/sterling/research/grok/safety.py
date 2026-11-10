"""Structural screen for Grok output.

The screen reads the locked Desk Rules and Stage 2 policy files and checks
their SHA-256 pins. It does not import the operational packages, write those
files, or submit an order. A failed pin, a short, an unknown name, leverage,
or a stated planned risk above the ceiling becomes a hold. Weights the
research gate would clip are rejected rather than silently resized.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from sterling.research.errors import ResearchError
from sterling.research.grok.preregistration import APPROVED_UNIVERSE
from sterling.research.grok.schema import FORBIDDEN_KEYS
from sterling.research.instruments import load_candidate_universe
from sterling.research.money import ZERO, parse_decimal
from sterling.research.risk_config import load_research_limits
from sterling.research.risk_gate import apply_risk_gate

PER_TRADE_CAP = Decimal("2.50")
DAILY_LOSS_CAP = Decimal("5.00")
PER_TRADE_FRACTION = Decimal("0.005")
DAILY_LOSS_FRACTION = Decimal("0.01")
RULES_FILE_SHA256 = "ba938baa22dd74944c9fe2cc7e68182c06498b3bc5fbff388df84818f6ae24ba"
POLICY_FILE_SHA256 = "5b5c315e57ebf65c3cd7291af11fd7cbe0bc897b2ee7d57f7a8051030246a363"
_PENNY = Decimal("0.01")
_APPROVED = frozenset(APPROVED_UNIVERSE)


def _rules_path() -> Path:
    return Path(__file__).resolve().parents[2] / "rules" / "data" / "desk-rules-v0.2.json"


def _policy_path() -> Path:
    return Path(__file__).resolve().parents[2] / "risk" / "data" / "stage2-gate1-policy-v1.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _penny_down(amount: Decimal) -> Decimal:
    quantized = amount.quantize(_PENNY, rounding=ROUND_DOWN)
    return quantized if quantized != 0 else Decimal("0.00")


def _day_loss(marked: Decimal, opening: Decimal, net: Decimal) -> Decimal:
    raw = opening - (marked - net)
    if raw <= 0:
        return Decimal("0.00")
    return raw


def _deny(reasons: list[str], *, budget_blocked: bool) -> dict[str, object]:
    return {
        "accepted_for_paper": False,
        "allowlist_permitted": False,
        "budget_blocked": budget_blocked,
        "daily_loss_cap_gbp": format(DAILY_LOSS_CAP, "f"),
        "execution_permission": "NOT_ISSUED",
        "orders_submitted": 0,
        "per_trade_cap_gbp": format(PER_TRADE_CAP, "f"),
        "reasons": reasons,
        "weights": {},
    }


def _pins_hold(rules_path: Path, policy_path: Path) -> str | None:
    try:
        rules_hash = _sha256(rules_path)
        policy_hash = _sha256(policy_path)
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "operational_pin_failed"
    if rules_hash != RULES_FILE_SHA256 or policy_hash != POLICY_FILE_SHA256:
        return "operational_pin_failed"
    if not isinstance(rules, dict) or not isinstance(policy, dict):
        return "operational_pin_failed"
    risk = rules.get("risk")
    orders = rules.get("orders")
    account = rules.get("account")
    budgets = policy.get("budgets")
    policy_orders = policy.get("orders")
    settings = policy.get("operational_numerical_settings")
    boundary = policy.get("gate_boundary")
    if not all(isinstance(item, dict) for item in (risk, orders, account, budgets, policy_orders, settings, boundary)):
        return "operational_pin_failed"
    assert isinstance(risk, dict)
    assert isinstance(orders, dict)
    assert isinstance(account, dict)
    assert isinstance(budgets, dict)
    assert isinstance(policy_orders, dict)
    assert isinstance(settings, dict)
    assert isinstance(boundary, dict)
    per_trade = risk.get("per_trade_planned_risk")
    daily = risk.get("daily_loss")
    if not isinstance(per_trade, dict) or not isinstance(daily, dict):
        return "operational_pin_failed"
    expected = (
        per_trade.get("absolute_ceiling") == "2.50",
        daily.get("absolute_ceiling") == "5.00",
        orders.get("instrument_allowlist") == [],
        orders.get("live_trading") is False,
        account.get("live") is False,
        budgets.get("per_trade_absolute_ceiling_gbp") == "2.50",
        budgets.get("daily_loss_absolute_ceiling_gbp") == "5.00",
        policy_orders.get("instrument_allowlist") == [],
        policy_orders.get("live_trading") is False,
        settings.get("default_decision") == "DENY",
        boundary.get("execution_permission") == "NOT_ISSUED",
    )
    if not all(expected):
        return "operational_pin_failed"
    return None


def _decimal(value: object) -> Decimal | None:
    try:
        return parse_decimal(value, label="field")
    except ResearchError:
        return None


def pinned_review(
    payload: Mapping[str, object],
    *,
    rules_path: Path | None = None,
    policy_path: Path | None = None,
) -> dict[str, object]:
    """Return a paper decision. Execution permission stays ``NOT_ISSUED``."""
    pin_reason = _pins_hold(rules_path or _rules_path(), policy_path or _policy_path())
    if pin_reason is not None:
        return _deny([pin_reason], budget_blocked=True)

    rejection = payload.get("rejection_reason")
    if isinstance(rejection, str) and rejection:
        return _deny([rejection], budget_blocked=False)

    quoted_raw = payload.get("quoted")
    if not isinstance(quoted_raw, list) or not all(isinstance(item, str) for item in quoted_raw):
        return _deny(["malformed_payload"], budget_blocked=True)
    quoted = set(quoted_raw)
    positions = _positions(payload.get("positions"))
    prices = _prices(payload.get("quotes"))
    account = _account(payload.get("account"))
    paper_nav = _decimal(payload.get("paper_nav_gbp"))
    instruments = payload.get("instruments")
    if positions is None or prices is None or account is None or paper_nav is None or paper_nav <= 0:
        return _deny(["malformed_payload"], budget_blocked=True)
    if not isinstance(instruments, list):
        return _deny(["malformed_payload"], budget_blocked=True)

    reasons: list[str] = []
    current = {name: market / paper_nav for name, (_quantity, market) in positions.items() if market > 0}
    proposed = dict(current)
    planned: list[Decimal] = []
    for raw in instruments:
        if not isinstance(raw, dict):
            return _deny(["malformed_payload"], budget_blocked=True)
        item = {str(key): value for key, value in raw.items()}
        blocked = sorted(set(item) & FORBIDDEN_KEYS)
        if blocked:
            return _deny(["forbidden_field:" + ",".join(blocked)], budget_blocked=True)
        instrument_id = item.get("instrument_id")
        action = item.get("action")
        if not isinstance(instrument_id, str) or not isinstance(action, str):
            return _deny(["malformed_payload"], budget_blocked=True)
        if instrument_id not in _APPROVED or instrument_id not in quoted:
            return _deny([f"unknown_instrument:{instrument_id}"], budget_blocked=True)
        target = item.get("target_weight")
        weight = None if target is None else _decimal(target)
        if action == "HOLD":
            if weight is not None:
                return _deny(["inconsistent_action"], budget_blocked=False)
            continue
        if action not in {"BUY", "SELL"} or weight is None:
            return _deny(["action_rejected"], budget_blocked=False)
        if weight < 0:
            return _deny(["negative_weight"], budget_blocked=False)
        if weight > 1:
            return _deny(["overweight"], budget_blocked=False)
        held = current.get(instrument_id, ZERO)
        if action == "BUY" and weight <= held:
            return _deny(["inconsistent_action"], budget_blocked=False)
        if action == "SELL" and (held <= 0 or weight >= held):
            return _deny(["short"], budget_blocked=False)
        size = item.get("size_request")
        if size is not None:
            quantity = _decimal(size)
            price = prices.get(instrument_id)
            held_qty = positions.get(instrument_id, (ZERO, ZERO))[0]
            if quantity is None or price is None or quantity < 0 or quantity * price > paper_nav:
                return _deny(["oversized_size"], budget_blocked=False)
            if action == "SELL" and quantity > held_qty:
                return _deny(["short"], budget_blocked=False)
        risk = item.get("planned_risk_gbp")
        if risk is not None:
            amount = _decimal(risk)
            if amount is None or amount <= 0:
                return _deny(["malformed_payload"], budget_blocked=True)
            planned.append(amount)
        if weight == 0:
            proposed.pop(instrument_id, None)
        else:
            proposed[instrument_id] = weight

    gross = sum(proposed.values(), ZERO)
    if gross > 1:
        return _deny(["gross_above_one"], budget_blocked=False)
    if any(weight < 0 for weight in proposed.values()):
        return _deny(["negative_weight"], budget_blocked=False)

    marked, opening, net = account
    per_trade_budget = _penny_down(min(marked * PER_TRADE_FRACTION, PER_TRADE_CAP))
    daily_budget = _penny_down(min(opening * DAILY_LOSS_FRACTION, DAILY_LOSS_CAP))
    loss = _day_loss(marked, opening, net)
    budget_blocked = False
    if planned:
        if any(amount > per_trade_budget for amount in planned):
            reasons.append("planned_risk_above_cap")
            budget_blocked = True
        if loss >= daily_budget or loss + sum(planned, ZERO) > daily_budget:
            reasons.append("day_loss_cap")
            budget_blocked = True
    if budget_blocked:
        return _deny(reasons, budget_blocked=True)

    limits = load_research_limits()
    classes = {instrument.ticker: instrument.asset_class for instrument in load_candidate_universe().instruments}
    gated = apply_risk_gate(proposed, current, limits, classes, paper_nav)
    if gated.veto or any(reason.startswith(("clipped ", "scaled turnover")) for reason in gated.reasons):
        reasons.append("research_gate")
        reasons.extend(gated.reasons)
        return _deny(reasons, budget_blocked=False)
    if not reasons:
        reasons.append("accepted")
    reasons.extend(reason for reason in gated.reasons if reason != "accepted")
    return {
        "accepted_for_paper": True,
        "allowlist_permitted": False,
        "budget_blocked": False,
        "daily_loss_cap_gbp": format(DAILY_LOSS_CAP, "f"),
        "execution_permission": "NOT_ISSUED",
        "orders_submitted": 0,
        "per_trade_cap_gbp": format(PER_TRADE_CAP, "f"),
        "reasons": reasons,
        "weights": {ticker: format(weight, "f") for ticker, weight in gated.weights if weight > 0},
    }


def _positions(value: object) -> dict[str, tuple[Decimal, Decimal]] | None:
    if not isinstance(value, list):
        return None
    positions: dict[str, tuple[Decimal, Decimal]] = {}
    for item in value:
        if not isinstance(item, dict):
            return None
        instrument_id = item.get("instrument_id")
        quantity = _decimal(item.get("quantity"))
        market = _decimal(item.get("market_value_gbp"))
        if not isinstance(instrument_id, str) or quantity is None or market is None or quantity < 0 or market < 0:
            return None
        positions[instrument_id] = (quantity, market)
    return positions


def _prices(value: object) -> dict[str, Decimal] | None:
    if not isinstance(value, list):
        return None
    prices: dict[str, Decimal] = {}
    for item in value:
        if not isinstance(item, dict):
            return None
        instrument_id = item.get("instrument_id")
        price = _decimal(item.get("price"))
        if not isinstance(instrument_id, str) or price is None or price <= 0:
            return None
        prices[instrument_id] = price
    return prices


def _account(value: object) -> tuple[Decimal, Decimal, Decimal] | None:
    if not isinstance(value, dict):
        return None
    marked = _decimal(value.get("marked_equity_gbp"))
    opening = _decimal(value.get("opening_equity_gbp"))
    net = _decimal(value.get("net_external_deposits_gbp"))
    if marked is None or opening is None or net is None or marked <= 0 or opening <= 0:
        return None
    return marked, opening, net

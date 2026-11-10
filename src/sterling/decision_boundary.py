"""Operational admission for a Grok payload.

The payload is data. This module loads Desk Rules v0.2 and the Stage 2 Gate 1
policy, runs the existing budget assessment when a planned risk is stated, and
always returns execution DENY. The instrument allowlist is empty, so nothing
is permitted. It does not submit an order, write the ledger, or edit policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sterling.ledger.calendar import london_date
from sterling.risk.contracts import (
    AccountInput,
    BaselineKind,
    ClockInput,
    EntryAssessment,
    EntryProposal,
    Freshness,
    KnownStatus,
    StopProtection,
)
from sterling.risk.policy import load_default_policy
from sterling.risk.validation import assess_long_entries
from sterling.rules.approved import DAILY_LOSS_ABSOLUTE_CEILING_GBP, PER_TRADE_ABSOLUTE_CEILING_GBP
from sterling.rules.loader import load_default_rules
from sterling.rules.model import instrument_allowed

RESEARCH_NAMES = frozenset({"VWRP", "CUKX", "IGLT", "AGBP", "SGLN", "XSTR"})
FX_MAJORS = frozenset({"USD", "EUR", "JPY", "CHF", "AUD", "CAD", "NZD", "SEK", "NOK"})
_KNOWN_NAMES = RESEARCH_NAMES | FX_MAJORS
_FORBIDDEN = frozenset(
    {
        "place_order",
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


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    if "e" in value.lower() or value.strip() != value or value.startswith("+"):
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    return amount


def _reasons(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def admit_grok_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Assess one payload. The returned permission bits are fixed false."""
    rules = load_default_rules()
    policy = load_default_policy()
    reasons: list[str] = ["ALLOWLIST_EMPTY", "EXECUTION_DENY"]
    structurally_blocked = False
    if rules.instrument_allowlist or rules.live_trading or rules.live or policy.live or policy.live_trading:
        structurally_blocked = True
        reasons.append("operational_pin_failed")
    if policy.per_trade_absolute_ceiling_gbp != PER_TRADE_ABSOLUTE_CEILING_GBP:
        structurally_blocked = True
    if policy.daily_loss_absolute_ceiling_gbp != DAILY_LOSS_ABSOLUTE_CEILING_GBP:
        structurally_blocked = True

    instruments = payload.get("instruments")
    rows = instruments if isinstance(instruments, list) else []
    if not isinstance(instruments, list):
        structurally_blocked = True
        reasons.append("malformed_payload")

    quoted_raw = payload.get("quoted")
    quoted = set(quoted_raw) if isinstance(quoted_raw, list) and all(isinstance(item, str) for item in quoted_raw) else set()
    positions = _position_values(payload.get("positions"))
    paper_nav = _decimal(payload.get("paper_nav_gbp"))
    current: dict[str, Decimal] = {}
    if positions is not None and paper_nav is not None and paper_nav > 0:
        current = {name: market / paper_nav for name, (_qty, market) in positions.items() if market > 0}
    elif rows:
        structurally_blocked = True
        reasons.append("malformed_payload")

    proposed = dict(current)
    planned: list[tuple[str, Decimal]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            structurally_blocked = True
            reasons.append("malformed_payload")
            continue
        item = {str(key): value for key, value in raw.items()}
        blocked = sorted(set(item) & _FORBIDDEN)
        if blocked:
            structurally_blocked = True
            reasons.append("forbidden_field:" + ",".join(blocked))
        instrument_id = item.get("instrument_id")
        action = item.get("action")
        if not isinstance(instrument_id, str) or instrument_id not in _KNOWN_NAMES or instrument_id not in quoted:
            structurally_blocked = True
            name = instrument_id if isinstance(instrument_id, str) else "missing"
            reasons.append(f"unknown_instrument:{name}")
            instrument_id = name if isinstance(instrument_id, str) else "MISSING"
        elif instrument_allowed(rules, instrument_id):
            structurally_blocked = True
            reasons.append("allowlist_not_empty")
        weight = _decimal(item.get("target_weight")) if item.get("target_weight") is not None else None
        held = current.get(instrument_id, Decimal(0)) if isinstance(instrument_id, str) else Decimal(0)
        if action == "SELL" and (weight is None or weight < 0 or held <= 0 or weight >= held):
            structurally_blocked = True
            reasons.append("short")
        if weight is not None and weight < 0:
            structurally_blocked = True
            reasons.append("negative_weight")
        if weight is not None and weight > 1:
            structurally_blocked = True
            reasons.append("overweight")
        if isinstance(instrument_id, str) and action in {"BUY", "SELL"} and weight is not None and weight >= 0:
            if weight == 0:
                proposed.pop(instrument_id, None)
            else:
                proposed[instrument_id] = weight
        risk = item.get("planned_risk_gbp")
        if risk is not None:
            amount = _decimal(risk)
            if isinstance(instrument_id, str) and amount is not None and amount > 0:
                planned.append((instrument_id, amount))
            else:
                structurally_blocked = True
                reasons.append("malformed_payload")
    if sum(proposed.values(), Decimal(0)) > 1:
        structurally_blocked = True
        reasons.append("gross_above_one")

    mathematical: bool | None = None
    budget_blocked = False
    if planned:
        assessment = _assess(payload, planned)
        if assessment is None:
            budget_blocked = True
            reasons.append("malformed_payload")
        else:
            mathematical = assessment.mathematical_budget_compliant
            budget_blocked = not mathematical
            reasons.append("stage2_gate1_reviewed")
            reasons.extend(finding.reason.value for finding in assessment.budget_findings)
            if assessment.execution_permission_issued or assessment.allowlist_permitted or assessment.ticket_issued:
                structurally_blocked = True
                budget_blocked = True
                reasons.append("screen_rejected")
    else:
        reasons.append("stage2_gate1_reviewed_no_stated_planned_risk")

    return {
        "allowlist_permitted": False,
        "budget_blocked": budget_blocked,
        "daily_loss_cap_gbp": format(DAILY_LOSS_ABSOLUTE_CEILING_GBP, "f"),
        "default_decision": "DENY",
        "execution_permission": "NOT_ISSUED",
        "mathematical_budget_compliant": mathematical,
        "orders_submitted": 0,
        "per_trade_cap_gbp": format(PER_TRADE_ABSOLUTE_CEILING_GBP, "f"),
        "reasons": reasons,
        "structurally_blocked": structurally_blocked,
    }


def combine_reviews(paper: Mapping[str, object], operational: Mapping[str, object]) -> dict[str, object]:
    """Keep a paper book only when both reviews allow it. Permission stays denied."""
    bits_ok = (
        paper.get("execution_permission") == "NOT_ISSUED"
        and operational.get("execution_permission") == "NOT_ISSUED"
        and paper.get("orders_submitted") == 0
        and operational.get("orders_submitted") == 0
        and paper.get("allowlist_permitted") is False
        and operational.get("allowlist_permitted") is False
        and paper.get("per_trade_cap_gbp") == "2.50"
        and paper.get("daily_loss_cap_gbp") == "5.00"
        and operational.get("per_trade_cap_gbp") == "2.50"
        and operational.get("daily_loss_cap_gbp") == "5.00"
    )
    blocked = operational.get("budget_blocked") is True or operational.get("structurally_blocked") is True
    accepted = paper.get("accepted_for_paper") is True and paper.get("budget_blocked") is not True and not blocked and bits_ok
    weights = _weights(paper.get("weights")) if accepted else {}
    if sum(weights.values(), Decimal(0)) > 1 or any(weight < 0 or weight > 1 for weight in weights.values()):
        accepted = False
        weights = {}
    reasons = _reasons(paper.get("reasons")) + _reasons(operational.get("reasons"))
    if not bits_ok:
        reasons.append("screen_rejected")
        accepted = False
        weights = {}
    return {
        "accepted_for_paper": accepted,
        "allowlist_permitted": False,
        "budget_blocked": operational.get("budget_blocked") is True or paper.get("budget_blocked") is True,
        "daily_loss_cap_gbp": "5.00",
        "execution_permission": "NOT_ISSUED",
        "mathematical_budget_compliant": operational.get("mathematical_budget_compliant"),
        "orders_submitted": 0,
        "per_trade_cap_gbp": "2.50",
        "reasons": reasons,
        "weights": {ticker: format(weight, "f") for ticker, weight in sorted(weights.items())},
    }


def _weights(value: object) -> dict[str, Decimal]:
    if not isinstance(value, dict):
        return {}
    weights: dict[str, Decimal] = {}
    for key, raw in value.items():
        amount = _decimal(raw)
        if not isinstance(key, str) or amount is None or amount <= 0:
            continue
        weights[key] = amount
    return weights


def _position_values(value: object) -> dict[str, tuple[Decimal, Decimal]] | None:
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


def _assess(payload: Mapping[str, object], planned: list[tuple[str, Decimal]]) -> EntryAssessment | None:
    account_raw = payload.get("account")
    as_of_raw = payload.get("as_of")
    if not isinstance(account_raw, dict) or not isinstance(as_of_raw, str):
        return None
    marked = _decimal(account_raw.get("marked_equity_gbp"))
    opening = _decimal(account_raw.get("opening_equity_gbp"))
    net = _decimal(account_raw.get("net_external_deposits_gbp"))
    if marked is None or opening is None or net is None or marked <= 0 or opening <= 0:
        return None
    try:
        as_of = datetime.fromisoformat(as_of_raw)
    except ValueError:
        return None
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        return None
    day = london_date(as_of)
    proposals: list[EntryProposal] = []
    for index, (instrument_id, risk) in enumerate(planned):
        entry = Decimal("1.00") + risk
        proposals.append(
            EntryProposal(
                proposal_id=f"G{index}-{instrument_id}",
                episode_id=f"E{index}-{instrument_id}",
                instrument_id=instrument_id,
                instrument_currency="GBP",
                product="LONG_EQUITY",
                side="BUY",
                borrowing=False,
                leverage=Decimal("1"),
                opens_new_episode=True,
                quantity=Decimal("1"),
                quantity_precision=0,
                entry_price=entry,
                stop_price=Decimal("1.00"),
                stop_protection=StopProtection.FULL,
                covered_quantity=Decimal("1"),
                fx_rate_to_gbp=Decimal("1"),
                conservative_price=entry,
                conservative_fx_rate_to_gbp=Decimal("1"),
                expected_entry_cost_gbp=Decimal("0.00"),
                expected_exit_cost_gbp=Decimal("0.00"),
                adverse_allowance_gbp=Decimal("0.00"),
            )
        )
    return assess_long_entries(
        policy=load_default_policy(),
        account=AccountInput(
            currency="GBP",
            live=False,
            simulated=True,
            marked_equity_gbp=marked,
            equity_freshness=Freshness.FRESH_VALIDATED,
            baseline_kind=BaselineKind.FIRST_FRESH_OPENING,
            opening_equity_gbp=opening,
            opening_freshness=Freshness.FRESH_VALIDATED,
            opening_london_date=day,
            as_of_london_date=day,
            net_external_deposits_gbp=net,
            available_cash_gbp=marked,
            available_cash_status=KnownStatus.KNOWN,
            gross_exposure_status=KnownStatus.KNOWN,
            daily_halt_latched=False,
            loss_streak_halt_latched=False,
        ),
        clock=ClockInput(as_of=as_of),
        proposals=tuple(proposals),
    )

"""Load and validate a desk-rules document.

Validation order is deliberate: live trading and raised ceilings are rejected
even when the editor also rewrites ``content_hash`` so the file looks consistent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

from sterling.errors import (
    CeilingRaised,
    LiveTradingRejected,
    MoneyError,
    RulesHashMismatch,
    RulesValidationError,
    UnapprovedRulesVersion,
)
from sterling.ledger.money import require_money, require_scale
from sterling.rules.approved import (
    APPROVED_CONTENT_SHA256,
    APPROVED_RULES_VERSION,
    APPROVED_RULES_VERSION_ID,
    APPROVED_SCHEMA_VERSION,
    DAILY_LOSS_ABSOLUTE_CEILING_GBP,
    DAILY_LOSS_EQUITY_FRACTION,
    PER_TRADE_ABSOLUTE_CEILING_GBP,
    PER_TRADE_EQUITY_FRACTION,
    STARTING_CAPITAL_GBP,
    TOTAL_OPEN_PLANNED_RISK_FRACTION,
)
from sterling.rules.hashing import content_hash
from sterling.rules.model import DeskRules

_TOP_KEYS = {
    "schema_version",
    "version",
    "version_id",
    "owner_approved",
    "description",
    "account",
    "risk",
    "products",
    "holding_period_trading_days",
    "orders",
    "day_boundary",
    "ceilings_policy",
    "content_hash",
}

_DAY_LOSS_FORMULA = "max(0, day_opening_equity - adjusted_current_equity)"
_ADJUSTED_EQUITY = "marked_to_market_minus_net_external_deposits_since_baseline"
_BASELINE = "first_fresh_validated_equity_snapshot"


def default_rules_text() -> str:
    resource = files("sterling.rules").joinpath("data/desk-rules-v0.2.json")
    return resource.read_text(encoding="utf-8")


def load_default_rules() -> DeskRules:
    return loads_rules(default_rules_text())


def load_rules(path: Path) -> DeskRules:
    return loads_rules(Path(path).read_text(encoding="utf-8"))


def loads_rules(text: str) -> DeskRules:
    try:
        raw = json.loads(text, parse_float=_reject_float)
    except json.JSONDecodeError as exc:
        raise RulesValidationError("rules document is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise RulesValidationError("rules document must be a JSON object")
    document: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise RulesValidationError("rules keys must be strings")
        document[key] = value
    return validate_rules_document(document)


def validate_rules_document(document: Mapping[str, Any]) -> DeskRules:
    data = dict(document)
    _exact_keys(data, _TOP_KEYS, "rules")
    schema_version = _string(data, "schema_version")
    version = _string(data, "version")
    version_id = _string(data, "version_id")
    owner_approved = _bool(data, "owner_approved")
    description = _string(data, "description")
    if description.strip() == "":
        raise RulesValidationError("description must not be empty")
    account = _object(data["account"], "account")
    risk = _object(data["risk"], "risk")
    products = _object(data["products"], "products")
    holding = _object(data["holding_period_trading_days"], "holding_period_trading_days")
    orders = _object(data["orders"], "orders")
    day_boundary = _object(data["day_boundary"], "day_boundary")
    ceilings_policy = _object(data["ceilings_policy"], "ceilings_policy")
    embedded_hash = _string(data, "content_hash")

    _exact_keys(
        account,
        {"currency", "starting_capital", "simulated", "live"},
        "account",
    )
    currency = _string_field(account, "currency", "account.currency")
    starting_capital = _money(account["starting_capital"], "account.starting_capital")
    simulated = _bool_field(account, "simulated", "account.simulated")
    live = _bool_field(account, "live", "account.live")

    _exact_keys(
        risk,
        {
            "per_trade_planned_risk",
            "daily_loss",
            "total_open_planned_risk",
            "consecutive_net_closed_loss_halt",
        },
        "risk",
    )
    per_trade = _object(risk["per_trade_planned_risk"], "risk.per_trade_planned_risk")
    daily = _object(risk["daily_loss"], "risk.daily_loss")
    open_risk = _object(risk["total_open_planned_risk"], "risk.total_open_planned_risk")
    streak = _object(
        risk["consecutive_net_closed_loss_halt"],
        "risk.consecutive_net_closed_loss_halt",
    )
    _exact_keys(
        per_trade,
        {
            "equity_fraction",
            "absolute_ceiling",
            "ceiling_currency",
            "includes_expected_costs",
            "includes_adverse_execution_allowance",
        },
        "risk.per_trade_planned_risk",
    )
    _exact_keys(
        daily,
        {
            "day_opening_equity_fraction",
            "absolute_ceiling",
            "ceiling_currency",
            "behaviour",
            "guaranteed_loss_cap",
        },
        "risk.daily_loss",
    )
    _exact_keys(open_risk, {"equity_fraction"}, "risk.total_open_planned_risk")
    _exact_keys(streak, {"threshold", "restart"}, "risk.consecutive_net_closed_loss_halt")

    per_trade_fraction = _decimal(per_trade["equity_fraction"], "risk.per_trade_planned_risk.equity_fraction")
    per_trade_ceiling = _money(
        per_trade["absolute_ceiling"],
        "risk.per_trade_planned_risk.absolute_ceiling",
    )
    per_trade_currency = _string_field(
        per_trade,
        "ceiling_currency",
        "risk.per_trade_planned_risk.ceiling_currency",
    )
    includes_costs = _bool_field(
        per_trade,
        "includes_expected_costs",
        "risk.per_trade_planned_risk.includes_expected_costs",
    )
    includes_adverse = _bool_field(
        per_trade,
        "includes_adverse_execution_allowance",
        "risk.per_trade_planned_risk.includes_adverse_execution_allowance",
    )
    daily_fraction = _decimal(
        daily["day_opening_equity_fraction"],
        "risk.daily_loss.day_opening_equity_fraction",
    )
    daily_ceiling = _money(daily["absolute_ceiling"], "risk.daily_loss.absolute_ceiling")
    daily_currency = _string_field(daily, "ceiling_currency", "risk.daily_loss.ceiling_currency")
    behaviour = _string_field(daily, "behaviour", "risk.daily_loss.behaviour")
    guaranteed = _bool_field(daily, "guaranteed_loss_cap", "risk.daily_loss.guaranteed_loss_cap")
    open_fraction = _decimal(open_risk["equity_fraction"], "risk.total_open_planned_risk.equity_fraction")
    loss_halt = _int_field(streak, "threshold", "risk.consecutive_net_closed_loss_halt.threshold")
    restart = _string_field(streak, "restart", "risk.consecutive_net_closed_loss_halt.restart")

    _exact_keys(
        products,
        {
            "long_only",
            "unleveraged",
            "borrowing",
            "shorts",
            "cfds",
            "futures",
            "options",
            "leveraged_or_inverse",
        },
        "products",
    )
    long_only = _bool_field(products, "long_only", "products.long_only")
    unleveraged = _bool_field(products, "unleveraged", "products.unleveraged")
    borrowing = _bool_field(products, "borrowing", "products.borrowing")
    shorts = _bool_field(products, "shorts", "products.shorts")
    cfds = _bool_field(products, "cfds", "products.cfds")
    futures = _bool_field(products, "futures", "products.futures")
    options = _bool_field(products, "options", "products.options")
    leveraged = _bool_field(products, "leveraged_or_inverse", "products.leveraged_or_inverse")

    _exact_keys(holding, {"min", "max", "provisional"}, "holding_period_trading_days")
    holding_min = _int_field(holding, "min", "holding_period_trading_days.min")
    holding_max = _int_field(holding, "max", "holding_period_trading_days.max")
    holding_provisional = _bool_field(
        holding,
        "provisional",
        "holding_period_trading_days.provisional",
    )

    _exact_keys(
        orders,
        {"regular_session_only", "live_trading", "instrument_allowlist"},
        "orders",
    )
    regular_only = _bool_field(orders, "regular_session_only", "orders.regular_session_only")
    live_trading = _bool_field(orders, "live_trading", "orders.live_trading")
    allowlist = _string_list(orders["instrument_allowlist"], "orders.instrument_allowlist")

    _exact_keys(
        day_boundary,
        {
            "timezone",
            "local_time",
            "baseline",
            "baseline_persisted",
            "adjusted_equity",
            "withdrawals",
            "day_loss",
            "fees_and_fx_counted_once_via_equity",
            "external_flows_do_not_reset_budget",
        },
        "day_boundary",
    )
    timezone_name = _string_field(day_boundary, "timezone", "day_boundary.timezone")
    local_time = _string_field(day_boundary, "local_time", "day_boundary.local_time")
    baseline = _string_field(day_boundary, "baseline", "day_boundary.baseline")
    baseline_persisted = _bool_field(
        day_boundary,
        "baseline_persisted",
        "day_boundary.baseline_persisted",
    )
    adjusted_equity = _string_field(day_boundary, "adjusted_equity", "day_boundary.adjusted_equity")
    withdrawals = _string_field(day_boundary, "withdrawals", "day_boundary.withdrawals")
    day_loss = _string_field(day_boundary, "day_loss", "day_boundary.day_loss")
    fees_once = _bool_field(
        day_boundary,
        "fees_and_fx_counted_once_via_equity",
        "day_boundary.fees_and_fx_counted_once_via_equity",
    )
    flows_do_not_reset = _bool_field(
        day_boundary,
        "external_flows_do_not_reset_budget",
        "day_boundary.external_flows_do_not_reset_budget",
    )

    _exact_keys(
        ceilings_policy,
        {"absolute", "changeable_only_by_new_owner_approved_rules_version"},
        "ceilings_policy",
    )
    ceilings_absolute = _bool_field(ceilings_policy, "absolute", "ceilings_policy.absolute")
    ceilings_versioned = _bool_field(
        ceilings_policy,
        "changeable_only_by_new_owner_approved_rules_version",
        "ceilings_policy.changeable_only_by_new_owner_approved_rules_version",
    )

    if live or live_trading:
        raise LiveTradingRejected("Live trading cannot be enabled in Stage 1. Config validation rejects live=true.")
    if per_trade_ceiling > PER_TRADE_ABSOLUTE_CEILING_GBP or daily_ceiling > DAILY_LOSS_ABSOLUTE_CEILING_GBP:
        raise CeilingRaised(
            "Absolute ceiling exceeds the owner-approved desk-rules-v0.2 ceiling "
            "(per-trade 2.50 GBP, daily loss 5.00 GBP). Ceilings change only with "
            "a new owner-approved rules version."
        )
    if allowlist:
        raise RulesValidationError("instrument allowlist must be empty in Stage 1; no instrument is tradable")
    if not long_only or not unleveraged or borrowing or shorts or cfds or futures or options or leveraged:
        raise RulesValidationError(
            "products must stay long-only, unleveraged, and free of borrowing, "
            "shorts, CFDs, futures, options, and leveraged or inverse products"
        )
    if guaranteed:
        raise RulesValidationError("daily loss behaviour is a reactive halt, not a guaranteed loss cap")
    if behaviour != "reactive_halt":
        raise RulesValidationError("daily loss behaviour must be reactive_halt")
    if restart != "human_only":
        raise RulesValidationError("a loss-streak halt can be restarted only by a human")
    if not simulated:
        raise RulesValidationError("the Stage 1 account must be simulated")
    if not regular_only:
        raise RulesValidationError("orders must be regular-session only")
    if schema_version != APPROVED_SCHEMA_VERSION or version != APPROVED_RULES_VERSION:
        raise UnapprovedRulesVersion(f"rules version {version!r} is not owner-approved; Stage 1 accepts only 0.2")
    if version_id != APPROVED_RULES_VERSION_ID:
        raise UnapprovedRulesVersion(f"rules version id {version_id!r} is not owner-approved; Stage 1 accepts only desk-rules-v0.2")
    if not owner_approved:
        raise UnapprovedRulesVersion("rules document is not marked owner-approved")

    _expect(currency == "GBP", "account currency must be GBP")
    _expect(starting_capital == STARTING_CAPITAL_GBP, "starting capital must be 500.00 GBP")
    _expect(per_trade_fraction == PER_TRADE_EQUITY_FRACTION, "per-trade fraction must be 0.005")
    _expect(
        per_trade_ceiling == PER_TRADE_ABSOLUTE_CEILING_GBP,
        "per-trade absolute ceiling must be 2.50 GBP",
    )
    _expect(per_trade_currency == "GBP", "per-trade ceiling currency must be GBP")
    _expect(includes_costs, "per-trade planned risk must include expected costs")
    _expect(includes_adverse, "per-trade planned risk must include adverse execution allowance")
    _expect(daily_fraction == DAILY_LOSS_EQUITY_FRACTION, "daily-loss fraction must be 0.01")
    _expect(daily_ceiling == DAILY_LOSS_ABSOLUTE_CEILING_GBP, "daily-loss absolute ceiling must be 5.00 GBP")
    _expect(daily_currency == "GBP", "daily-loss ceiling currency must be GBP")
    _expect(open_fraction == TOTAL_OPEN_PLANNED_RISK_FRACTION, "open planned-risk fraction must be 0.01")
    _expect(loss_halt == 3, "consecutive net closed-loss halt must be 3")
    _expect(holding_min == 2 and holding_max == 10, "holding period must be 2-10 trading days")
    _expect(holding_provisional, "holding period is provisional in v0.2")
    _expect(timezone_name == "Europe/London", "day boundary timezone must be Europe/London")
    _expect(local_time == "00:00", "day boundary must be 00:00 Europe/London")
    _expect(baseline == _BASELINE, "baseline definition does not match v0.2")
    _expect(baseline_persisted, "day-opening baseline must be persisted")
    _expect(adjusted_equity == _ADJUSTED_EQUITY, "adjusted-equity definition does not match v0.2")
    _expect(withdrawals == "negative_external_flow", "withdrawals must count as negative external flow")
    _expect(day_loss == _DAY_LOSS_FORMULA, "day-loss formula does not match v0.2")
    _expect(fees_once, "fees and FX must be counted once via equity")
    _expect(flows_do_not_reset, "deposits and withdrawals must not reset the daily-loss budget")
    _expect(ceilings_absolute, "ceilings must be absolute")
    _expect(
        ceilings_versioned,
        "ceilings must be changeable only by a new owner-approved rules version",
    )

    recomputed = content_hash(data)
    if embedded_hash != recomputed:
        raise RulesHashMismatch(f"content_hash does not match the canonical document body. Recomputed {recomputed}.")
    if recomputed != APPROVED_CONTENT_SHA256:
        raise RulesHashMismatch(
            "content_hash is not the owner-approved desk-rules-v0.2 digest. "
            "Editing the file requires a new owner-approved rules version."
        )

    return DeskRules(
        schema_version=schema_version,
        version=version,
        version_id=version_id,
        content_hash=embedded_hash,
        currency=currency,
        starting_capital=starting_capital,
        simulated=simulated,
        live=False,
        per_trade_equity_fraction=per_trade_fraction,
        per_trade_absolute_ceiling=per_trade_ceiling,
        per_trade_includes_expected_costs=includes_costs,
        per_trade_includes_adverse_execution_allowance=includes_adverse,
        daily_loss_equity_fraction=daily_fraction,
        daily_loss_absolute_ceiling=daily_ceiling,
        daily_loss_behaviour=behaviour,
        daily_loss_guaranteed_cap=False,
        total_open_planned_risk_equity_fraction=open_fraction,
        consecutive_loss_halt=loss_halt,
        halt_restart=restart,
        long_only=long_only,
        unleveraged=unleveraged,
        borrowing_allowed=borrowing,
        shorts_allowed=shorts,
        cfds_allowed=cfds,
        futures_allowed=futures,
        options_allowed=options,
        leveraged_or_inverse_allowed=leveraged,
        holding_period_min_days=holding_min,
        holding_period_max_days=holding_max,
        holding_period_provisional=holding_provisional,
        regular_session_only=regular_only,
        live_trading=False,
        instrument_allowlist=tuple(allowlist),
        day_boundary_timezone=timezone_name,
        day_boundary_local_time=local_time,
    )


def _reject_float(value: str) -> float:
    raise RulesValidationError("numeric rules values must be JSON strings or integers, not floats: " + value)


def _exact_keys(data: Mapping[str, Any], expected: set[str], path: str) -> None:
    keys = set(data)
    missing = expected - keys
    unknown = keys - expected
    if missing or unknown:
        raise RulesValidationError(f"{path} keys mismatch; missing={sorted(missing)} unknown={sorted(unknown)}")


def _object(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RulesValidationError(f"{path} must be an object")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RulesValidationError(f"{path} keys must be strings")
        result[key] = item
    return result


def _string(data: Mapping[str, Any], key: str) -> str:
    return _string_field(data, key, key)


def _string_field(data: Mapping[str, Any], key: str, path: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise RulesValidationError(f"{path} must be a string")
    return value


def _bool(data: Mapping[str, Any], key: str) -> bool:
    return _bool_field(data, key, key)


def _bool_field(data: Mapping[str, Any], key: str, path: str) -> bool:
    value = data[key]
    if not isinstance(value, bool):
        raise RulesValidationError(f"{path} must be a boolean")
    return value


def _int_field(data: Mapping[str, Any], key: str, path: str) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RulesValidationError(f"{path} must be an integer")
    return value


def _decimal(value: object, path: str) -> Any:
    if not isinstance(value, str):
        raise RulesValidationError(f"{path} must be a decimal string")
    try:
        return require_scale(value, label=path)
    except MoneyError as exc:
        raise RulesValidationError(str(exc)) from exc


def _money(value: object, path: str) -> Any:
    if not isinstance(value, str):
        raise RulesValidationError(f"{path} must be a decimal string")
    try:
        return require_money(value, label=path)
    except MoneyError as exc:
        raise RulesValidationError(str(exc)) from exc


def _string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list):
        raise RulesValidationError(f"{path} must be a list")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise RulesValidationError(f"{path}[{index}] must be a string")
        items.append(item)
    return items


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise RulesValidationError(message)

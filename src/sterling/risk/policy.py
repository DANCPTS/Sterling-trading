"""Load the owner-approved Stage 2 Gate 1 policy.

The document is a separate version from Desk Rules v0.2. Loading it checks
the Stage 1 content pin and does not rewrite that pin. Numerical quote, FX,
session, and source settings cannot be stored as approved numbers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any

from sterling.errors import MoneyError
from sterling.ledger.money import require_money, require_scale
from sterling.risk.approved import (
    CONCENTRATION_EQUITY_FRACTION,
    CONSECUTIVE_NET_LOSS_EPISODES,
    DAILY_LOSS_ABSOLUTE_CEILING_GBP,
    DAILY_LOSS_EQUITY_FRACTION,
    MAX_SIMULTANEOUS_POSITION_EPISODES,
    OPERATIONAL_DEFAULT_DECISION,
    OPERATIONAL_UNAPPROVED,
    PER_TRADE_ABSOLUTE_CEILING_GBP,
    PER_TRADE_EQUITY_FRACTION,
    POLICY_CONTENT_SHA256,
    POLICY_GATE,
    POLICY_SCHEMA_VERSION,
    POLICY_VERSION,
    POLICY_VERSION_ID,
    STAGE1_APPROVED_CONTENT_SHA256,
    STAGE1_APPROVED_RULES_VERSION_ID,
    STARTING_CAPITAL_GBP,
    TOTAL_OPEN_PLANNED_RISK_FRACTION,
)
from sterling.risk.errors import PolicyError
from sterling.rules.hashing import content_hash

_TOP_KEYS = {
    "schema_version",
    "version",
    "version_id",
    "owner_approved",
    "gate",
    "description",
    "stage1_rules_version_id",
    "stage1_content_hash",
    "account",
    "budgets",
    "products",
    "orders",
    "day_boundary",
    "operational_numerical_settings",
    "gate_boundary",
    "content_hash",
}

_ACCOUNT_KEYS = {"currency", "starting_capital", "simulated", "live"}

_BUDGET_KEYS = {
    "per_trade_equity_fraction",
    "per_trade_absolute_ceiling_gbp",
    "per_trade_basis",
    "per_trade_includes_expected_costs",
    "per_trade_includes_adverse_allowance",
    "daily_loss_equity_fraction",
    "daily_loss_absolute_ceiling_gbp",
    "daily_loss_basis",
    "daily_loss_guaranteed_cap",
    "aggregate_open_planned_risk_equity_fraction",
    "aggregate_includes_pending",
    "concentration_equity_fraction",
    "concentration_includes_current_and_pending_notional",
    "concentration_price_basis",
    "max_simultaneous_position_episodes",
    "episodes_include_pending_openings",
    "reentry_after_flat_is_distinct",
    "partial_fill_or_exit_is_not_new_episode",
    "consecutive_net_loss_episode_threshold",
    "loss_streak_restart",
    "available_budget_rounding",
    "risk_rounding",
    "day_loss_formula",
}

_PRODUCT_KEYS = {
    "long_only",
    "unleveraged",
    "borrowing",
    "shorts",
    "cfds",
    "futures",
    "options",
    "leveraged_or_inverse",
}

_ORDER_KEYS = {"live_trading", "instrument_allowlist", "regular_session_only"}
_DAY_KEYS = {"timezone", "local_time", "opening_baseline"}
_OPERATIONAL_KEYS = {
    "quote_max_age",
    "fx_max_age",
    "session_clock",
    "market_calendar",
    "source_quality",
    "default_decision",
    "fixture_policy_usable_as_owner_policy",
}
_BOUNDARY_KEYS = {
    "runtime_enforcement",
    "halt_persistence",
    "reset_application",
    "ticket_issuance",
    "atomic_stop_execution",
    "os_isolation_verified",
    "simulator_execution",
    "execution_permission",
    "planned_cap_guarantees_realised_loss",
    "stop_contract_proves_protection",
}

_DAY_LOSS_FORMULA = "max(0, day_opening_equity - (marked_equity - net_external_deposits_since_baseline))"
_SENTINEL_FIELDS = ("quote_max_age", "fx_max_age", "session_clock", "market_calendar", "source_quality")


@dataclass(frozen=True, slots=True)
class Stage2Policy:
    """Owner-approved Gate 1 numbers. Operational ages are not included."""

    schema_version: str
    version: str
    version_id: str
    content_hash: str
    stage1_rules_version_id: str
    stage1_content_hash: str
    currency: str
    starting_capital_gbp: Decimal
    simulated: bool
    live: bool
    per_trade_equity_fraction: Decimal
    per_trade_absolute_ceiling_gbp: Decimal
    daily_loss_equity_fraction: Decimal
    daily_loss_absolute_ceiling_gbp: Decimal
    aggregate_equity_fraction: Decimal
    concentration_equity_fraction: Decimal
    max_simultaneous_position_episodes: int
    consecutive_loss_episode_threshold: int
    instrument_allowlist: tuple[str, ...]
    long_only: bool
    unleveraged: bool
    borrowing_allowed: bool
    shorts_allowed: bool
    cfds_allowed: bool
    futures_allowed: bool
    options_allowed: bool
    leveraged_or_inverse_allowed: bool
    live_trading: bool
    regular_session_only: bool
    day_boundary_timezone: str
    day_boundary_local_time: str
    operational_settings_state: str
    default_operational_decision: str
    fixture_policy_usable_as_owner_policy: bool
    runtime_enforcement: bool
    halt_persistence: bool
    reset_application: bool
    ticket_issuance: bool
    atomic_stop_execution: bool
    os_isolation_verified: bool
    simulator_execution: bool
    execution_permission: str
    planned_cap_guarantees_realised_loss: bool
    stop_contract_proves_protection: bool

    def __post_init__(self) -> None:
        if (
            self.version_id != POLICY_VERSION_ID
            or self.version != POLICY_VERSION
            or self.schema_version != POLICY_SCHEMA_VERSION
            or self.content_hash != POLICY_CONTENT_SHA256
            or self.stage1_rules_version_id != STAGE1_APPROVED_RULES_VERSION_ID
            or self.stage1_content_hash != STAGE1_APPROVED_CONTENT_SHA256
        ):
            raise PolicyError("policy identity does not match the owner-approved Stage 2 Gate 1 pin")
        if self.live or self.live_trading or not self.simulated:
            raise PolicyError("live trading cannot be enabled on the Stage 2 Gate 1 policy")
        if (
            self.per_trade_equity_fraction != PER_TRADE_EQUITY_FRACTION
            or self.per_trade_absolute_ceiling_gbp != PER_TRADE_ABSOLUTE_CEILING_GBP
            or self.daily_loss_equity_fraction != DAILY_LOSS_EQUITY_FRACTION
            or self.daily_loss_absolute_ceiling_gbp != DAILY_LOSS_ABSOLUTE_CEILING_GBP
            or self.aggregate_equity_fraction != TOTAL_OPEN_PLANNED_RISK_FRACTION
            or self.concentration_equity_fraction != CONCENTRATION_EQUITY_FRACTION
            or self.max_simultaneous_position_episodes != MAX_SIMULTANEOUS_POSITION_EPISODES
            or self.consecutive_loss_episode_threshold != CONSECUTIVE_NET_LOSS_EPISODES
            or self.starting_capital_gbp != STARTING_CAPITAL_GBP
        ):
            raise PolicyError(
                "Stage 2 Gate 1 caps must stay at 0.5% / 2.50 GBP, 1% / 5.00 GBP, 1% aggregate, 25% concentration, and 4 episodes"
            )
        if self.instrument_allowlist != () or self.currency != "GBP":
            raise PolicyError("the instrument allowlist stays empty and the currency stays GBP")
        if (
            not self.long_only
            or not self.unleveraged
            or self.borrowing_allowed
            or self.shorts_allowed
            or self.cfds_allowed
            or self.futures_allowed
            or self.options_allowed
            or self.leveraged_or_inverse_allowed
            or not self.regular_session_only
            or self.day_boundary_timezone != "Europe/London"
            or self.day_boundary_local_time != "00:00"
        ):
            raise PolicyError("product and day-boundary flags do not match the owner-approved policy")
        if (
            self.operational_settings_state != OPERATIONAL_UNAPPROVED
            or self.default_operational_decision != OPERATIONAL_DEFAULT_DECISION
            or self.fixture_policy_usable_as_owner_policy
            or self.execution_permission != "NOT_ISSUED"
        ):
            raise PolicyError("operational numerical settings are UNAPPROVED_UNCONFIGURED and default to DENY")
        if (
            self.runtime_enforcement
            or self.halt_persistence
            or self.reset_application
            or self.ticket_issuance
            or self.atomic_stop_execution
            or self.os_isolation_verified
            or self.simulator_execution
            or self.planned_cap_guarantees_realised_loss
            or self.stop_contract_proves_protection
        ):
            raise PolicyError("Gate 1 policy cannot claim runtime enforcement, tickets, stops, or isolation")


def default_policy_text() -> str:
    resource = files("sterling.risk").joinpath("data/stage2-gate1-policy-v1.json")
    return resource.read_text(encoding="utf-8")


def load_default_policy() -> Stage2Policy:
    return loads_policy(default_policy_text())


def load_policy(path: Path) -> Stage2Policy:
    return loads_policy(Path(path).read_text(encoding="utf-8"))


def loads_policy(text: str) -> Stage2Policy:
    try:
        raw = json.loads(text, parse_float=_reject_float)
    except json.JSONDecodeError as exc:
        raise PolicyError("policy document is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise PolicyError("policy document must be a JSON object")
    document: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise PolicyError("policy keys must be strings")
        document[key] = value
    return validate_policy_document(document)


def validate_policy_document(document: Mapping[str, Any]) -> Stage2Policy:
    data = dict(document)
    _exact_keys(data, _TOP_KEYS, "policy")
    schema_version = _string(data, "schema_version")
    version = _string(data, "version")
    version_id = _string(data, "version_id")
    owner_approved = _bool(data, "owner_approved")
    gate = _string(data, "gate")
    description = _string(data, "description")
    stage1_version = _string(data, "stage1_rules_version_id")
    stage1_hash = _string(data, "stage1_content_hash")
    embedded_hash = _string(data, "content_hash")
    if description.strip() == "":
        raise PolicyError("description must not be empty")
    if not owner_approved:
        raise PolicyError("the Stage 2 Gate 1 policy file must be the owner-approved document")
    if gate != POLICY_GATE or version_id != POLICY_VERSION_ID or version != POLICY_VERSION:
        raise PolicyError("only stage2-gate1-policy-v1 can be loaded as the Gate 1 policy")
    if schema_version != POLICY_SCHEMA_VERSION:
        raise PolicyError("unsupported policy schema")

    account = _object(data["account"], "account")
    budgets = _object(data["budgets"], "budgets")
    products = _object(data["products"], "products")
    orders = _object(data["orders"], "orders")
    day_boundary = _object(data["day_boundary"], "day_boundary")
    operational = _object(data["operational_numerical_settings"], "operational_numerical_settings")
    boundary = _object(data["gate_boundary"], "gate_boundary")
    _exact_keys(account, _ACCOUNT_KEYS, "account")
    _exact_keys(budgets, _BUDGET_KEYS, "budgets")
    _exact_keys(products, _PRODUCT_KEYS, "products")
    _exact_keys(orders, _ORDER_KEYS, "orders")
    _exact_keys(day_boundary, _DAY_KEYS, "day_boundary")
    _exact_keys(operational, _OPERATIONAL_KEYS, "operational_numerical_settings")
    _exact_keys(boundary, _BOUNDARY_KEYS, "gate_boundary")

    currency = _string_field(account, "currency", "account.currency")
    starting = _money(account["starting_capital"], "account.starting_capital")
    simulated = _bool_field(account, "simulated", "account.simulated")
    live = _bool_field(account, "live", "account.live")
    if live or not simulated:
        raise PolicyError("live trading cannot be enabled on the Stage 2 Gate 1 policy")

    per_trade_fraction = _decimal(budgets["per_trade_equity_fraction"], "budgets.per_trade_equity_fraction")
    per_trade_ceiling = _money(budgets["per_trade_absolute_ceiling_gbp"], "budgets.per_trade_absolute_ceiling_gbp")
    daily_fraction = _decimal(budgets["daily_loss_equity_fraction"], "budgets.daily_loss_equity_fraction")
    daily_ceiling = _money(budgets["daily_loss_absolute_ceiling_gbp"], "budgets.daily_loss_absolute_ceiling_gbp")
    aggregate_fraction = _decimal(
        budgets["aggregate_open_planned_risk_equity_fraction"],
        "budgets.aggregate_open_planned_risk_equity_fraction",
    )
    concentration_fraction = _decimal(budgets["concentration_equity_fraction"], "budgets.concentration_equity_fraction")
    episodes = _int_field(budgets, "max_simultaneous_position_episodes", "budgets.max_simultaneous_position_episodes")
    loss_episodes = _int_field(
        budgets,
        "consecutive_net_loss_episode_threshold",
        "budgets.consecutive_net_loss_episode_threshold",
    )
    _expect(
        _string_field(budgets, "per_trade_basis", "budgets.per_trade_basis") == "fresh_validated_current_equity",
        "per-trade risk must use fresh validated current equity",
    )
    _expect(
        _string_field(budgets, "daily_loss_basis", "budgets.daily_loss_basis") == "fresh_validated_day_opening_equity",
        "daily threshold must use fresh validated day-opening equity",
    )
    _expect(
        _bool_field(budgets, "per_trade_includes_expected_costs", "budgets.per_trade_includes_expected_costs"),
        "per-trade risk must include expected costs",
    )
    _expect(
        _bool_field(budgets, "per_trade_includes_adverse_allowance", "budgets.per_trade_includes_adverse_allowance"),
        "per-trade risk must include the adverse allowance",
    )
    if _bool_field(budgets, "daily_loss_guaranteed_cap", "budgets.daily_loss_guaranteed_cap"):
        raise PolicyError("the daily-loss figure is not a guaranteed cap")
    _expect(
        _bool_field(budgets, "aggregate_includes_pending", "budgets.aggregate_includes_pending"),
        "aggregate risk must include pending risk",
    )
    _expect(
        _bool_field(
            budgets,
            "concentration_includes_current_and_pending_notional",
            "budgets.concentration_includes_current_and_pending_notional",
        ),
        "concentration must include current and pending notional",
    )
    _expect(
        _string_field(budgets, "concentration_price_basis", "budgets.concentration_price_basis")
        == "conservative_supplied_price_and_fx",
        "concentration must use the conservative supplied price and FX",
    )
    _expect(
        _bool_field(budgets, "episodes_include_pending_openings", "budgets.episodes_include_pending_openings"),
        "episode count must include pending openings",
    )
    _expect(
        _bool_field(budgets, "reentry_after_flat_is_distinct", "budgets.reentry_after_flat_is_distinct"),
        "a re-entry after flat is a distinct episode",
    )
    _expect(
        _bool_field(budgets, "partial_fill_or_exit_is_not_new_episode", "budgets.partial_fill_or_exit_is_not_new_episode"),
        "a partial fill or exit is not a new episode",
    )
    _expect(
        _string_field(budgets, "loss_streak_restart", "budgets.loss_streak_restart") == "owner_only",
        "loss-streak restart must be owner_only",
    )
    _expect(
        _string_field(budgets, "available_budget_rounding", "budgets.available_budget_rounding") == "down",
        "available budgets round down",
    )
    _expect(_string_field(budgets, "risk_rounding", "budgets.risk_rounding") == "up", "risk rounds up")
    _expect(
        _string_field(budgets, "day_loss_formula", "budgets.day_loss_formula") == _DAY_LOSS_FORMULA,
        "day-loss formula does not match the owner-approved expression",
    )
    if (
        per_trade_fraction != PER_TRADE_EQUITY_FRACTION
        or per_trade_ceiling != PER_TRADE_ABSOLUTE_CEILING_GBP
        or daily_fraction != DAILY_LOSS_EQUITY_FRACTION
        or daily_ceiling != DAILY_LOSS_ABSOLUTE_CEILING_GBP
        or aggregate_fraction != TOTAL_OPEN_PLANNED_RISK_FRACTION
        or concentration_fraction != CONCENTRATION_EQUITY_FRACTION
        or episodes != MAX_SIMULTANEOUS_POSITION_EPISODES
        or loss_episodes != CONSECUTIVE_NET_LOSS_EPISODES
        or starting != STARTING_CAPITAL_GBP
    ):
        raise PolicyError(
            "caps do not match the owner-approved pins; a new number needs a new policy version, "
            "and rewriting content_hash does not approve it"
        )

    long_only = _bool_field(products, "long_only", "products.long_only")
    unleveraged = _bool_field(products, "unleveraged", "products.unleveraged")
    borrowing = _bool_field(products, "borrowing", "products.borrowing")
    shorts = _bool_field(products, "shorts", "products.shorts")
    cfds = _bool_field(products, "cfds", "products.cfds")
    futures = _bool_field(products, "futures", "products.futures")
    options = _bool_field(products, "options", "products.options")
    leveraged = _bool_field(products, "leveraged_or_inverse", "products.leveraged_or_inverse")
    if not long_only or not unleveraged or borrowing or shorts or cfds or futures or options or leveraged:
        raise PolicyError("product flags must stay long-only, unleveraged, and free of listed derivatives")

    live_trading = _bool_field(orders, "live_trading", "orders.live_trading")
    allowlist = _string_list(orders["instrument_allowlist"], "orders.instrument_allowlist")
    regular_only = _bool_field(orders, "regular_session_only", "orders.regular_session_only")
    if live_trading or allowlist or not regular_only:
        raise PolicyError("orders stay simulated, the allowlist stays empty, and the session flag stays on")

    timezone_name = _string_field(day_boundary, "timezone", "day_boundary.timezone")
    local_time = _string_field(day_boundary, "local_time", "day_boundary.local_time")
    opening = _string_field(day_boundary, "opening_baseline", "day_boundary.opening_baseline")
    if timezone_name != "Europe/London" or local_time != "00:00":
        raise PolicyError("day boundary stays 00:00 Europe/London")
    if opening != "first_fresh_validated_opening_snapshot":
        raise PolicyError("opening baseline must be the first fresh validated opening snapshot")

    for field in _SENTINEL_FIELDS:
        value = operational[field]
        if isinstance(value, (int, float)) or not isinstance(value, str) or value != OPERATIONAL_UNAPPROVED:
            raise PolicyError(
                "operational numerical settings are UNAPPROVED_UNCONFIGURED and cannot be given a number "
                f"or a fixture value ({field})"
            )
    decision = _string_field(operational, "default_decision", "operational_numerical_settings.default_decision")
    fixture_ok = _bool_field(
        operational,
        "fixture_policy_usable_as_owner_policy",
        "operational_numerical_settings.fixture_policy_usable_as_owner_policy",
    )
    if decision != OPERATIONAL_DEFAULT_DECISION or fixture_ok:
        raise PolicyError("unapproved operational settings default to DENY and fixture policy is not owner policy")

    runtime = _bool_field(boundary, "runtime_enforcement", "gate_boundary.runtime_enforcement")
    persistence = _bool_field(boundary, "halt_persistence", "gate_boundary.halt_persistence")
    reset_application = _bool_field(boundary, "reset_application", "gate_boundary.reset_application")
    tickets = _bool_field(boundary, "ticket_issuance", "gate_boundary.ticket_issuance")
    stops = _bool_field(boundary, "atomic_stop_execution", "gate_boundary.atomic_stop_execution")
    isolation = _bool_field(boundary, "os_isolation_verified", "gate_boundary.os_isolation_verified")
    simulator = _bool_field(boundary, "simulator_execution", "gate_boundary.simulator_execution")
    permission = _string_field(boundary, "execution_permission", "gate_boundary.execution_permission")
    guarantees = _bool_field(
        boundary,
        "planned_cap_guarantees_realised_loss",
        "gate_boundary.planned_cap_guarantees_realised_loss",
    )
    proves = _bool_field(boundary, "stop_contract_proves_protection", "gate_boundary.stop_contract_proves_protection")
    if (
        runtime
        or persistence
        or reset_application
        or tickets
        or stops
        or isolation
        or simulator
        or guarantees
        or proves
        or permission != "NOT_ISSUED"
    ):
        raise PolicyError("gate boundary flags cannot claim enforcement, tickets, protection, or isolation")

    if stage1_version != STAGE1_APPROVED_RULES_VERSION_ID or stage1_hash != STAGE1_APPROVED_CONTENT_SHA256:
        raise PolicyError("Stage 2 policy must cite the unchanged Desk Rules v0.2 pin")

    actual_hash = content_hash(dict(data))
    if actual_hash != embedded_hash:
        raise PolicyError("policy content_hash does not match the document")
    if embedded_hash != POLICY_CONTENT_SHA256:
        raise PolicyError(
            "content_hash is not the owner-approved Stage 2 Gate 1 digest. "
            "Editing the file requires a new owner-approved policy version."
        )

    return Stage2Policy(
        schema_version=schema_version,
        version=version,
        version_id=version_id,
        content_hash=embedded_hash,
        stage1_rules_version_id=stage1_version,
        stage1_content_hash=stage1_hash,
        currency=currency,
        starting_capital_gbp=starting,
        simulated=True,
        live=False,
        per_trade_equity_fraction=per_trade_fraction,
        per_trade_absolute_ceiling_gbp=per_trade_ceiling,
        daily_loss_equity_fraction=daily_fraction,
        daily_loss_absolute_ceiling_gbp=daily_ceiling,
        aggregate_equity_fraction=aggregate_fraction,
        concentration_equity_fraction=concentration_fraction,
        max_simultaneous_position_episodes=episodes,
        consecutive_loss_episode_threshold=loss_episodes,
        instrument_allowlist=tuple(allowlist),
        long_only=True,
        unleveraged=True,
        borrowing_allowed=False,
        shorts_allowed=False,
        cfds_allowed=False,
        futures_allowed=False,
        options_allowed=False,
        leveraged_or_inverse_allowed=False,
        live_trading=False,
        regular_session_only=True,
        day_boundary_timezone=timezone_name,
        day_boundary_local_time=local_time,
        operational_settings_state=OPERATIONAL_UNAPPROVED,
        default_operational_decision=OPERATIONAL_DEFAULT_DECISION,
        fixture_policy_usable_as_owner_policy=False,
        runtime_enforcement=False,
        halt_persistence=False,
        reset_application=False,
        ticket_issuance=False,
        atomic_stop_execution=False,
        os_isolation_verified=False,
        simulator_execution=False,
        execution_permission="NOT_ISSUED",
        planned_cap_guarantees_realised_loss=False,
        stop_contract_proves_protection=False,
    )


def _reject_float(value: str) -> float:
    raise PolicyError("numeric policy values must be JSON strings or integers, not floats: " + value)


def _exact_keys(data: Mapping[str, Any], expected: set[str], path: str) -> None:
    keys = set(data)
    missing = expected - keys
    unknown = keys - expected
    if missing or unknown:
        raise PolicyError(f"{path} keys mismatch; missing={sorted(missing)} unknown={sorted(unknown)}")


def _object(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{path} must be an object")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise PolicyError(f"{path} keys must be strings")
        result[key] = item
    return result


def _string(data: Mapping[str, Any], key: str) -> str:
    return _string_field(data, key, key)


def _string_field(data: Mapping[str, Any], key: str, path: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise PolicyError(f"{path} must be a string")
    return value


def _bool(data: Mapping[str, Any], key: str) -> bool:
    return _bool_field(data, key, key)


def _bool_field(data: Mapping[str, Any], key: str, path: str) -> bool:
    value = data[key]
    if not isinstance(value, bool):
        raise PolicyError(f"{path} must be a boolean")
    return value


def _int_field(data: Mapping[str, Any], key: str, path: str) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{path} must be an integer")
    return value


def _decimal(value: object, path: str) -> Decimal:
    if not isinstance(value, str):
        raise PolicyError(f"{path} must be a decimal string")
    try:
        amount = require_scale(value, label=path)
    except MoneyError as exc:
        raise PolicyError(str(exc)) from exc
    return amount


def _money(value: object, path: str) -> Decimal:
    if not isinstance(value, str):
        raise PolicyError(f"{path} must be a decimal string")
    try:
        amount = require_money(value, label=path)
    except MoneyError as exc:
        raise PolicyError(str(exc)) from exc
    return amount


def _string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list):
        raise PolicyError(f"{path} must be a list")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise PolicyError(f"{path}[{index}] must be a string")
        items.append(item)
    return items


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyError(message)

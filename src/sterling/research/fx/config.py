"""Pre-registered FX research configuration.

Parameters in ``fx-research-v1.json`` were fixed before the sample was scored.
Loading an operational rules id, live trading, leverage, or short selling is refused.
``owner_approved`` stays false: this file is not Desk Rules and not Stage 2 policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib.resources import files

from sterling.research.errors import ResearchError
from sterling.research.instruments import OPERATIONAL_VERSION_IDS
from sterling.research.money import parse_decimal
from sterling.research.risk_config import RESEARCH_STATUS

EXECUTION_PERMISSION = "NOT_ISSUED"
ORDERS_SUBMITTED = 0


@dataclass(frozen=True, slots=True)
class CurrencySpec:
    code: str
    name: str
    boe_series: str
    bis_area: str


@dataclass(frozen=True, slots=True)
class DateWindow:
    name: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class CostScenario:
    name: str
    platform_fee_round_trip: Decimal
    spread_set: str

    @property
    def fee_one_way(self) -> Decimal:
        return self.platform_fee_round_trip / Decimal(2)


@dataclass(frozen=True, slots=True)
class DepositScenario:
    name: str
    gbp_reference: str
    gbp_haircut: Decimal
    foreign_haircut: Decimal
    floor_at_zero: bool
    foreign_forced_rate: Decimal | None
    label: str


@dataclass(frozen=True, slots=True)
class FxResearchConfig:
    version_id: str
    status: str
    owner_approved: bool
    live_trading: bool
    execution_permission: str
    orders_submitted: int
    leverage: str
    short_selling: str
    book: str
    gross_exposure_max: Decimal
    starting_nav_gbp: Decimal
    day_count: str
    min_month_ends: int
    oos_fraction: Decimal
    stale_rate_days: int
    early_spread_before: date
    early_spread_multiplier: Decimal
    crisis_spread_multiplier: Decimal
    bootstrap_block_months: int
    bootstrap_replications: int
    bootstrap_seed: int
    bootstrap_ci: Decimal
    walk_train_months: int
    walk_test_months: int
    walk_step_months: int
    primary_cost_scenario: str
    primary_deposit_scenario: str
    currencies: tuple[CurrencySpec, ...]
    retail_half_spread: tuple[tuple[str, Decimal], ...]
    institutional_half_spread: tuple[tuple[str, Decimal], ...]
    spread_crisis_windows: tuple[DateWindow, ...]
    performance_windows: tuple[DateWindow, ...]
    cost_scenarios: tuple[CostScenario, ...]
    deposit_scenarios: tuple[DepositScenario, ...]
    trend_months: int
    trend_sensitivity_months: tuple[int, ...]
    return_months: int
    vol_window: int
    vol_min: int
    carry_k: int
    carry_threshold: Decimal
    carry_sensitivity_k: tuple[int, ...]
    carry_sensitivity_threshold: tuple[Decimal, ...]
    source_notes: tuple[str, ...]

    def cost(self, name: str) -> CostScenario:
        for scenario in self.cost_scenarios:
            if scenario.name == name:
                return scenario
        raise ResearchError(f"unknown FX cost scenario {name}")

    def deposit(self, name: str) -> DepositScenario:
        for scenario in self.deposit_scenarios:
            if scenario.name == name:
                return scenario
        raise ResearchError(f"unknown FX deposit scenario {name}")

    def half_spread(self, code: str, day: date, scenario: CostScenario) -> Decimal:
        table = self.retail_half_spread if scenario.spread_set == "retail" else self.institutional_half_spread
        base = _lookup(table, code, "half-spread")
        multiplier = Decimal(1)
        if day < self.early_spread_before:
            multiplier = self.early_spread_multiplier
        for window in self.spread_crisis_windows:
            if window.start <= day <= window.end:
                multiplier = max(multiplier, self.crisis_spread_multiplier)
        spread = base * multiplier
        if spread < 0 or spread >= 1:
            raise ResearchError("half-spread must sit in [0, 1)")
        return spread


def _lookup(pairs: tuple[tuple[str, Decimal], ...], code: str, label: str) -> Decimal:
    for name, value in pairs:
        if name == code:
            return value
    raise ResearchError(f"no {label} for {code}")


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ResearchError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResearchError(f"{label} must be a non-empty string")
    return value


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ResearchError(f"{label} must be a boolean")
    return value


def _int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResearchError(f"{label} must be an integer")
    return value


def _date(value: object, label: str) -> date:
    text = _string(value, label)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ResearchError(f"{label} must be an ISO date") from exc


def _windows(value: object, label: str) -> tuple[DateWindow, ...]:
    if not isinstance(value, list) or not value:
        raise ResearchError(f"{label} must be a non-empty list")
    windows: list[DateWindow] = []
    for index, item in enumerate(value):
        body = _object(item, f"{label}[{index}]")
        window = DateWindow(_string(body.get("name"), "name"), _date(body.get("start"), "start"), _date(body.get("end"), "end"))
        if window.end < window.start:
            raise ResearchError(f"{window.name} ends before it starts")
        windows.append(window)
    return tuple(windows)


def _spread_table(value: object, codes: set[str], label: str) -> tuple[tuple[str, Decimal], ...]:
    body = _object(value, label)
    if set(body) != codes:
        raise ResearchError(f"{label} must match the FX universe")
    rows: list[tuple[str, Decimal]] = []
    for code in sorted(body):
        spread = parse_decimal(body[code], label=code)
        if spread < 0 or spread >= 1:
            raise ResearchError(f"{code} half-spread must sit in [0, 1)")
        rows.append((code, spread))
    return tuple(rows)


def parse_fx_config(document: dict[str, object]) -> FxResearchConfig:
    version_id = document.get("version_id")
    if version_id in OPERATIONAL_VERSION_IDS or document.get("gate") == "stage2-gate1":
        raise ResearchError("refusing to load operational configuration as FX research")
    if document.get("status") != RESEARCH_STATUS:
        raise ResearchError("FX research status is not the research-simulation approval")
    if document.get("owner_approved") is not False:
        raise ResearchError("FX research config is not owner-approved; refusing owner_approved true")
    if document.get("live_trading") is not False:
        raise ResearchError("FX research refuses live trading")
    if document.get("execution_permission") != EXECUTION_PERMISSION or document.get("orders_submitted") != ORDERS_SUBMITTED:
        raise ResearchError("FX research cannot issue execution permission or a non-zero order count")
    if document.get("leverage") != "none":
        raise ResearchError("FX research refuses leverage")
    if document.get("short_selling") != "prohibited":
        raise ResearchError("FX research refuses short selling")
    gross = parse_decimal(document.get("gross_exposure_max"), label="gross_exposure_max")
    if gross != Decimal(1):
        raise ResearchError("unleveraged FX gross exposure max must be 1")
    if document.get("day_count") != "ACT/365":
        raise ResearchError("FX accrual day count must be ACT/365")
    raw_universe = document.get("universe")
    if not isinstance(raw_universe, list) or not raw_universe:
        raise ResearchError("FX universe must be a non-empty list")
    currencies: list[CurrencySpec] = []
    for index, item in enumerate(raw_universe):
        body = _object(item, f"universe[{index}]")
        currencies.append(
            CurrencySpec(
                code=_string(body.get("code"), "code"),
                name=_string(body.get("name"), "name"),
                boe_series=_string(body.get("boe_series"), "boe_series"),
                bis_area=_string(body.get("bis_area"), "bis_area"),
            )
        )
    codes = {item.code for item in currencies}
    if len(codes) != len(currencies):
        raise ResearchError("FX currency codes must be unique")
    bootstrap = _object(document.get("bootstrap"), "bootstrap")
    walk = _object(document.get("walk_forward"), "walk_forward")
    trend = _object(document.get("trend"), "trend")
    carry = _object(document.get("carry"), "carry")
    if trend.get("rule") != "sma" or trend.get("weighting") != "equal":
        raise ResearchError("primary trend rule must stay SMA and equal weight")
    months = trend.get("sensitivity_months")
    if not isinstance(months, list) or not all(isinstance(item, int) and not isinstance(item, bool) for item in months):
        raise ResearchError("trend sensitivity months must be integers")
    sensitivity_k = carry.get("sensitivity_k")
    if not isinstance(sensitivity_k, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in sensitivity_k
    ):
        raise ResearchError("carry sensitivity k must be integers")
    threshold_values = carry.get("sensitivity_threshold")
    if not isinstance(threshold_values, list):
        raise ResearchError("carry sensitivity thresholds must be a list")
    raw_costs = document.get("cost_scenarios")
    raw_deposits = document.get("deposit_scenarios")
    if not isinstance(raw_costs, list) or not isinstance(raw_deposits, list):
        raise ResearchError("cost and deposit scenarios must be lists")
    cost_scenarios: list[CostScenario] = []
    for index, item in enumerate(raw_costs):
        body = _object(item, f"cost_scenarios[{index}]")
        spread_set = _string(body.get("spread_set"), "spread_set")
        if spread_set not in {"retail", "institutional"}:
            raise ResearchError("spread set must be retail or institutional")
        fee = parse_decimal(body.get("platform_fee_round_trip"), label="platform_fee_round_trip")
        if fee < 0:
            raise ResearchError("platform fee cannot be negative")
        cost_scenarios.append(CostScenario(_string(body.get("name"), "name"), fee, spread_set))
    deposit_scenarios: list[DepositScenario] = []
    for index, item in enumerate(raw_deposits):
        body = _object(item, f"deposit_scenarios[{index}]")
        reference = _string(body.get("gbp_reference"), "gbp_reference")
        if reference not in {"sonia", "bank_rate"}:
            raise ResearchError("GBP deposit reference must be sonia or bank_rate")
        forced = body.get("foreign_forced_rate")
        forced_rate = None if forced is None else parse_decimal(forced, label="foreign_forced_rate")
        deposit_scenarios.append(
            DepositScenario(
                name=_string(body.get("name"), "name"),
                gbp_reference=reference,
                gbp_haircut=parse_decimal(body.get("gbp_haircut"), label="gbp_haircut"),
                foreign_haircut=parse_decimal(body.get("foreign_haircut"), label="foreign_haircut"),
                floor_at_zero=_bool(body.get("floor_at_zero"), "floor_at_zero"),
                foreign_forced_rate=forced_rate,
                label=_string(body.get("label"), "label"),
            )
        )
    notes = document.get("source_notes")
    if not isinstance(notes, list) or not all(isinstance(item, str) for item in notes):
        raise ResearchError("source notes must be strings")
    config = FxResearchConfig(
        version_id=_string(version_id, "version_id"),
        status=RESEARCH_STATUS,
        owner_approved=False,
        live_trading=False,
        execution_permission=EXECUTION_PERMISSION,
        orders_submitted=ORDERS_SUBMITTED,
        leverage="none",
        short_selling="prohibited",
        book=_string(document.get("book"), "book"),
        gross_exposure_max=gross,
        starting_nav_gbp=parse_decimal(document.get("starting_nav_gbp"), label="starting_nav_gbp"),
        day_count="ACT/365",
        min_month_ends=_int(document.get("min_month_ends"), "min_month_ends"),
        oos_fraction=parse_decimal(document.get("oos_fraction"), label="oos_fraction"),
        stale_rate_days=_int(document.get("stale_rate_days"), "stale_rate_days"),
        early_spread_before=_date(document.get("early_spread_before"), "early_spread_before"),
        early_spread_multiplier=parse_decimal(document.get("early_spread_multiplier"), label="early_spread_multiplier"),
        crisis_spread_multiplier=parse_decimal(document.get("crisis_spread_multiplier"), label="crisis_spread_multiplier"),
        bootstrap_block_months=_int(bootstrap.get("block_months"), "block_months"),
        bootstrap_replications=_int(bootstrap.get("replications"), "replications"),
        bootstrap_seed=_int(bootstrap.get("seed"), "seed"),
        bootstrap_ci=parse_decimal(bootstrap.get("ci"), label="ci"),
        walk_train_months=_int(walk.get("train_months"), "train_months"),
        walk_test_months=_int(walk.get("test_months"), "test_months"),
        walk_step_months=_int(walk.get("step_months"), "step_months"),
        primary_cost_scenario=_string(document.get("primary_cost_scenario"), "primary_cost_scenario"),
        primary_deposit_scenario=_string(document.get("primary_deposit_scenario"), "primary_deposit_scenario"),
        currencies=tuple(currencies),
        retail_half_spread=_spread_table(document.get("retail_half_spread"), codes, "retail_half_spread"),
        institutional_half_spread=_spread_table(document.get("institutional_half_spread"), codes, "institutional_half_spread"),
        spread_crisis_windows=_windows(document.get("spread_crisis_windows"), "spread_crisis_windows"),
        performance_windows=_windows(document.get("performance_windows"), "performance_windows"),
        cost_scenarios=tuple(cost_scenarios),
        deposit_scenarios=tuple(deposit_scenarios),
        trend_months=_int(trend.get("months"), "months"),
        trend_sensitivity_months=tuple(int(item) for item in months),
        return_months=_int(trend.get("return_months"), "return_months"),
        vol_window=_int(trend.get("vol_window"), "vol_window"),
        vol_min=_int(trend.get("vol_min"), "vol_min"),
        carry_k=_int(carry.get("k"), "k"),
        carry_threshold=parse_decimal(carry.get("threshold"), label="threshold"),
        carry_sensitivity_k=tuple(int(item) for item in sensitivity_k),
        carry_sensitivity_threshold=tuple(parse_decimal(item, label="sensitivity_threshold") for item in threshold_values),
        source_notes=tuple(str(item) for item in notes),
    )
    if config.min_month_ends < 13:
        raise ResearchError("the common sample needs 13 month-ends so the 12-month return uses no future")
    if not Decimal(0) < config.oos_fraction < 1:
        raise ResearchError("OOS fraction must sit between 0 and 1")
    if config.trend_months < 1 or config.carry_k < 1:
        raise ResearchError("trend lookback and carry k must be at least 1")
    config.cost(config.primary_cost_scenario)
    config.deposit(config.primary_deposit_scenario)
    return config


def load_fx_config() -> FxResearchConfig:
    resource = files("sterling.research.fx").joinpath("data/fx-research-v1.json")
    document = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ResearchError("FX research config must be an object")
    return parse_fx_config({str(key): value for key, value in document.items()})

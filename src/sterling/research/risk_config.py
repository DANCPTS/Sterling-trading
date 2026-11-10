"""Load the research-only risk file.

The status string is the research-simulation approval. ``owner_approved``
stays false: these limits are not Desk Rules and not the Stage 2 policy.
Loading an operational version id, or setting ``owner_approved`` true, is refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files

from sterling.research.errors import ResearchError
from sterling.research.instruments import OPERATIONAL_VERSION_IDS
from sterling.research.money import parse_decimal

RESEARCH_STATUS = "APPROVED FOR RESEARCH SIMULATION ONLY by owner 2026-11-09; not operational"


@dataclass(frozen=True, slots=True)
class ResearchLimits:
    status: str
    owner_approved: bool
    long_only: bool
    max_gross: Decimal
    max_single_instrument_weight: Decimal
    max_asset_class_weight: tuple[tuple[str, Decimal], ...]
    cash_min: Decimal
    cash_max: Decimal
    min_trade_notional_gbp: Decimal
    max_one_way_turnover: Decimal
    rebalance: str
    starting_nav_gbp: Decimal
    notes: tuple[str, ...]
    single_instrument_cap_exempt_asset_classes: tuple[str, ...] = ()
    risk_off_turnover_exception: bool = True

    def asset_cap(self, asset_class: str) -> Decimal:
        for name, cap in self.max_asset_class_weight:
            if name == asset_class:
                return cap
        raise ResearchError(f"no proposed asset-class cap for {asset_class}")

    def summary_lines(self) -> tuple[str, ...]:
        classes = ", ".join(f"{name} {format(cap, 'f')}" for name, cap in self.max_asset_class_weight)
        return (
            f"status: {self.status}",
            f"owner_approved: {self.owner_approved}",
            "long_only: true; gross exposure <= 1; cash weight 0 to 1",
            f"max single instrument: {format(self.max_single_instrument_weight, 'f')}",
            "single-name cap exempt asset classes: " + ", ".join(self.single_instrument_cap_exempt_asset_classes),
            f"max asset class: {classes}",
            f"min trade notional GBP: {format(self.min_trade_notional_gbp, 'f')}",
            f"max one-way turnover: {format(self.max_one_way_turnover, 'f')}",
            f"risk-off turnover exception: {self.risk_off_turnover_exception}",
            f"rebalance: {self.rebalance}",
            f"research starting NAV GBP: {format(self.starting_nav_gbp, 'f')}",
        )


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ResearchError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def parse_research_limits(document: dict[str, object]) -> ResearchLimits:
    version_id = document.get("version_id")
    if version_id in OPERATIONAL_VERSION_IDS or document.get("gate") == "stage2-gate1":
        raise ResearchError("refusing to load operational risk configuration as research limits")
    if document.get("status") != RESEARCH_STATUS:
        raise ResearchError(f"research limits status must be {RESEARCH_STATUS!r}")
    if document.get("owner_approved") is not False:
        raise ResearchError("research limits are not owner-approved; refusing owner_approved true")
    constraints = _object(document.get("constraints"), "constraints")
    account = _object(document.get("account"), "account")
    classes_raw = _object(constraints.get("max_asset_class_weight"), "max_asset_class_weight")
    classes = tuple((name, parse_decimal(cap, label=name)) for name, cap in sorted(classes_raw.items()))
    notes_raw = constraints.get("notes", [])
    if not isinstance(notes_raw, list):
        raise ResearchError("notes must be a list")
    exempt_raw = constraints.get("single_instrument_cap_exempt_asset_classes", [])
    if not isinstance(exempt_raw, list) or not all(isinstance(item, str) for item in exempt_raw):
        raise ResearchError("single_instrument_cap_exempt_asset_classes must be a list of strings")
    limits = ResearchLimits(
        status=RESEARCH_STATUS,
        owner_approved=False,
        long_only=constraints.get("long_only") is True and constraints.get("shorts") is False,
        max_gross=parse_decimal(constraints.get("max_gross_exposure"), label="max_gross_exposure"),
        max_single_instrument_weight=parse_decimal(
            constraints.get("max_single_instrument_weight"), label="max_single_instrument_weight"
        ),
        max_asset_class_weight=classes,
        cash_min=parse_decimal(constraints.get("cash_weight_min"), label="cash_weight_min"),
        cash_max=parse_decimal(constraints.get("cash_weight_max"), label="cash_weight_max"),
        min_trade_notional_gbp=parse_decimal(constraints.get("min_trade_notional_gbp"), label="min_trade_notional_gbp"),
        max_one_way_turnover=parse_decimal(constraints.get("max_one_way_turnover"), label="max_one_way_turnover"),
        rebalance=str(constraints.get("rebalance")),
        starting_nav_gbp=parse_decimal(account.get("research_starting_nav_gbp"), label="research_starting_nav_gbp"),
        notes=tuple(str(item) for item in notes_raw),
        single_instrument_cap_exempt_asset_classes=tuple(str(item) for item in exempt_raw),
        risk_off_turnover_exception=constraints.get("risk_off_turnover_exception") is not False,
    )
    if not limits.long_only or limits.max_gross > 1 or limits.cash_min < 0 or limits.cash_max > 1:
        raise ResearchError("research limits must stay long-only with gross and cash inside 0 to 1")
    if limits.max_single_instrument_weight <= 0 or limits.max_single_instrument_weight > 1:
        raise ResearchError("max single instrument weight must be inside (0, 1]")
    return limits


def load_research_limits() -> ResearchLimits:
    resource = files("sterling.research").joinpath("data/research-risk-v0.json")
    loaded = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ResearchError("research limits must be a JSON object")
    return parse_research_limits({str(key): value for key, value in loaded.items()})

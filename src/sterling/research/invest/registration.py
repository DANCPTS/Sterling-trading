"""Pre-registration for paper-v2. It does not alter grok-forward-v1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from sterling.research.grok.preregistration import (
    APPROVED_UNIVERSE,
    MIN_CALENDAR_MONTHS,
    MIN_CLOSED_ROUND_TRIPS,
    MIN_WEEKLY_DECISIONS,
    PILOT_LABEL,
)

EXPERIMENT_ID = "paper-v2"
POLICY = "paper-v2"
STARTING_CASH_GBP = Decimal("10000.00")
SUM_TOLERANCE = Decimal("0.005")
NO_TRADE_BAND = Decimal("0.005")
V1_STATUS = "closed after 1 week"


@dataclass(frozen=True, slots=True)
class PaperV2Registration:
    registration_id: str = EXPERIMENT_ID
    policy: str = POLICY
    status_of_v1: str = V1_STATUS
    cadence: str = "weekly"
    fill: str = "next_session_open"
    universe: tuple[str, ...] = APPROVED_UNIVERSE
    starting_cash_gbp: str = format(STARTING_CASH_GBP, "f")
    sum_tolerance: str = format(SUM_TOLERANCE, "f")
    no_trade_band: str = format(NO_TRADE_BAND, "f")
    min_weekly_decisions: int = MIN_WEEKLY_DECISIONS
    min_calendar_months: int = MIN_CALENDAR_MONTHS
    min_closed_round_trips: int = MIN_CLOSED_ROUND_TRIPS
    operational_caps: str = "not_applied"
    execution_permission: str = "NOT_ISSUED"

    def as_log_fields(self) -> dict[str, object]:
        return {
            "cadence": self.cadence,
            "execution_permission": self.execution_permission,
            "fill": self.fill,
            "min_calendar_months": self.min_calendar_months,
            "min_closed_round_trips": self.min_closed_round_trips,
            "min_weekly_decisions": self.min_weekly_decisions,
            "no_trade_band": self.no_trade_band,
            "operational_caps": self.operational_caps,
            "policy": self.policy,
            "registration_id": self.registration_id,
            "starting_cash_gbp": self.starting_cash_gbp,
            "status_of_v1": self.status_of_v1,
            "sum_tolerance": self.sum_tolerance,
            "universe": list(self.universe),
        }

    def digest(self) -> str:
        text = json.dumps(self.as_log_fields(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def phase_label(*, weeks: int, months: int, closed_round_trips: int) -> str:
    if weeks < MIN_WEEKLY_DECISIONS or months < MIN_CALENDAR_MONTHS or closed_round_trips < MIN_CLOSED_ROUND_TRIPS:
        return PILOT_LABEL
    return "pre-registered window"

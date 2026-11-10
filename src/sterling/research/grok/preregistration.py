"""Pre-registered forward comparison. The code is the registry.

Changing a field changes the digest stored in each new log. A short sample
can still be described. It cannot be reported as a conclusion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

REGISTRATION_ID = "grok-forward-v1"
APPROVED_UNIVERSE = ("VWRP", "CUKX", "IGLT", "AGBP", "SGLN", "XSTR")
FX_MAJORS = ("USD", "EUR", "JPY", "CHF", "AUD", "CAD", "NZD", "SEK", "NOK")
PRIMARY_ARM = "grok"
PRIMARY_BENCHMARK = "buy_and_hold_equal"
MIN_WEEKLY_DECISIONS = 104
MIN_CALENDAR_MONTHS = 24
MIN_CLOSED_ROUND_TRIPS = 30
BLOCK_WEEKS = 4
BOOTSTRAP_REPLICATIONS = 2000
BOOTSTRAP_SEED = 20261110
CONFIDENCE_LEVEL = Decimal("0.95")
WEEKS_PER_YEAR = Decimal(52)
PILOT_LABEL = "technical pilot, not evidence of profitability"


@dataclass(frozen=True, slots=True)
class PreRegistration:
    registration_id: str = REGISTRATION_ID
    cadence: str = "weekly"
    fill: str = "next_session_open"
    cost_model: str = "research_cost_model"
    universe: tuple[str, ...] = APPROVED_UNIVERSE
    fx_majors: tuple[str, ...] = FX_MAJORS
    arms: tuple[str, ...] = (
        "grok",
        "etf_trend",
        "etf_dual_momentum",
        "fx_carry",
        "buy_and_hold_equal",
        "gbp_cash",
    )
    primary_arm: str = PRIMARY_ARM
    primary_benchmark: str = PRIMARY_BENCHMARK
    metrics: tuple[str, ...] = (
        "total_return",
        "max_drawdown",
        "annualised_volatility",
        "sharpe",
        "trades",
        "hit_rate",
        "confidence_calibration",
    )
    min_weekly_decisions: int = MIN_WEEKLY_DECISIONS
    min_calendar_months: int = MIN_CALENDAR_MONTHS
    min_closed_round_trips: int = MIN_CLOSED_ROUND_TRIPS
    test: str = "circular_block_bootstrap_weekly_excess"
    block_weeks: int = BLOCK_WEEKS
    bootstrap_replications: int = BOOTSTRAP_REPLICATIONS
    bootstrap_seed: int = BOOTSTRAP_SEED
    confidence_level: str = format(CONFIDENCE_LEVEL, "f")
    frozen_trend_months: int = 10
    frozen_dual_momentum_months: int = 12
    frozen_dual_momentum_top_k: int = 2
    frozen_fx_carry_k: int = 3
    frozen_fx_carry_threshold: str = "0.005"

    def as_log_fields(self) -> dict[str, object]:
        return {
            "arms": list(self.arms),
            "block_weeks": self.block_weeks,
            "bootstrap_replications": self.bootstrap_replications,
            "bootstrap_seed": self.bootstrap_seed,
            "cadence": self.cadence,
            "confidence_level": self.confidence_level,
            "cost_model": self.cost_model,
            "fill": self.fill,
            "frozen_dual_momentum_months": self.frozen_dual_momentum_months,
            "frozen_dual_momentum_top_k": self.frozen_dual_momentum_top_k,
            "frozen_fx_carry_k": self.frozen_fx_carry_k,
            "frozen_fx_carry_threshold": self.frozen_fx_carry_threshold,
            "frozen_trend_months": self.frozen_trend_months,
            "fx_majors": list(self.fx_majors),
            "metrics": list(self.metrics),
            "min_calendar_months": self.min_calendar_months,
            "min_closed_round_trips": self.min_closed_round_trips,
            "min_weekly_decisions": self.min_weekly_decisions,
            "primary_arm": self.primary_arm,
            "primary_benchmark": self.primary_benchmark,
            "registration_id": self.registration_id,
            "test": self.test,
            "universe": list(self.universe),
        }

    def digest(self) -> str:
        text = json.dumps(self.as_log_fields(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def phase_label(*, weeks: int, months: int, closed_round_trips: int) -> str:
    """Pilot outputs stay descriptive. The registration thresholds are not lowered."""
    if weeks < MIN_WEEKLY_DECISIONS or months < MIN_CALENDAR_MONTHS or closed_round_trips < MIN_CLOSED_ROUND_TRIPS:
        return PILOT_LABEL
    return "pre-registered window"


def may_conclude(
    *,
    weeks: int,
    months: int,
    closed_round_trips: int,
    interval_low: Decimal,
    interval_high: Decimal,
) -> bool:
    """True only after the minimum sample and a 95% interval that excludes zero."""
    if weeks < MIN_WEEKLY_DECISIONS or months < MIN_CALENDAR_MONTHS or closed_round_trips < MIN_CLOSED_ROUND_TRIPS:
        return False
    return interval_high < 0 or interval_low > 0

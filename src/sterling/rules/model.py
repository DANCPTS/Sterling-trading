"""In-memory desk rules.

Objects can be built only with the owner-approved v0.2 figures. The loader is
the supported way to obtain one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sterling.errors import CeilingRaised, LiveTradingRejected, RulesValidationError
from sterling.rules.approved import (
    APPROVED_CONTENT_SHA256,
    APPROVED_RULES_VERSION,
    APPROVED_RULES_VERSION_ID,
    DAILY_LOSS_ABSOLUTE_CEILING_GBP,
    DAILY_LOSS_EQUITY_FRACTION,
    PER_TRADE_ABSOLUTE_CEILING_GBP,
    PER_TRADE_EQUITY_FRACTION,
    STARTING_CAPITAL_GBP,
    TOTAL_OPEN_PLANNED_RISK_FRACTION,
)


@dataclass(frozen=True, slots=True)
class DeskRules:
    """Owner-approved Desk Rules v0.2."""

    schema_version: str
    version: str
    version_id: str
    content_hash: str
    currency: str
    starting_capital: Decimal
    simulated: bool
    live: bool
    per_trade_equity_fraction: Decimal
    per_trade_absolute_ceiling: Decimal
    per_trade_includes_expected_costs: bool
    per_trade_includes_adverse_execution_allowance: bool
    daily_loss_equity_fraction: Decimal
    daily_loss_absolute_ceiling: Decimal
    daily_loss_behaviour: str
    daily_loss_guaranteed_cap: bool
    total_open_planned_risk_equity_fraction: Decimal
    consecutive_loss_halt: int
    halt_restart: str
    long_only: bool
    unleveraged: bool
    borrowing_allowed: bool
    shorts_allowed: bool
    cfds_allowed: bool
    futures_allowed: bool
    options_allowed: bool
    leveraged_or_inverse_allowed: bool
    holding_period_min_days: int
    holding_period_max_days: int
    holding_period_provisional: bool
    regular_session_only: bool
    live_trading: bool
    instrument_allowlist: tuple[str, ...]
    day_boundary_timezone: str
    day_boundary_local_time: str

    def __post_init__(self) -> None:
        if self.live or self.live_trading:
            raise LiveTradingRejected("Live trading cannot be enabled in Stage 1. Config validation rejects live=true.")
        if self.version_id != APPROVED_RULES_VERSION_ID or self.version != APPROVED_RULES_VERSION:
            raise RulesValidationError("only desk-rules-v0.2 can be constructed in Stage 1")
        if (
            self.per_trade_absolute_ceiling > PER_TRADE_ABSOLUTE_CEILING_GBP
            or self.daily_loss_absolute_ceiling > DAILY_LOSS_ABSOLUTE_CEILING_GBP
        ):
            raise CeilingRaised(
                "Absolute ceilings cannot be raised above 2.50 GBP per trade "
                "and 5.00 GBP daily loss without a new owner-approved rules version."
            )
        if (
            self.per_trade_absolute_ceiling != PER_TRADE_ABSOLUTE_CEILING_GBP
            or self.daily_loss_absolute_ceiling != DAILY_LOSS_ABSOLUTE_CEILING_GBP
            or self.per_trade_equity_fraction != PER_TRADE_EQUITY_FRACTION
            or self.daily_loss_equity_fraction != DAILY_LOSS_EQUITY_FRACTION
            or self.total_open_planned_risk_equity_fraction != TOTAL_OPEN_PLANNED_RISK_FRACTION
            or self.starting_capital != STARTING_CAPITAL_GBP
            or self.content_hash != APPROVED_CONTENT_SHA256
            or self.instrument_allowlist != ()
            or self.currency != "GBP"
            or not self.simulated
            or self.daily_loss_guaranteed_cap
            or self.daily_loss_behaviour != "reactive_halt"
            or self.halt_restart != "human_only"
            or self.consecutive_loss_halt != 3
            or not self.long_only
            or not self.unleveraged
            or self.borrowing_allowed
            or self.shorts_allowed
            or self.cfds_allowed
            or self.futures_allowed
            or self.options_allowed
            or self.leveraged_or_inverse_allowed
            or not self.regular_session_only
            or self.holding_period_min_days != 2
            or self.holding_period_max_days != 10
            or not self.holding_period_provisional
            or self.day_boundary_timezone != "Europe/London"
            or self.day_boundary_local_time != "00:00"
        ):
            raise RulesValidationError("rules do not match the owner-approved desk-rules-v0.2 pins")


def instrument_allowed(rules: DeskRules, instrument_id: str) -> bool:
    """Return whether an instrument is on the allowlist.

    The v0.2 allowlist is empty, so this is false for every id. The ledger does
    not consult it; a later order gateway must.
    """
    return instrument_id in rules.instrument_allowlist

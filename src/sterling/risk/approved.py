"""Owner-approved pins for the Stage 2 Gate 1 policy.

The Stage 1 desk-rules pin is imported and checked, not rewritten.
Concentration at 25% of validated equity and four simultaneous position
episodes are the only new numerical caps. Quote age, FX age, session
clocks, calendars, and source-quality numbers are not approved here.
"""

from __future__ import annotations

from decimal import Decimal

from sterling.rules.approved import (
    APPROVED_CONTENT_SHA256 as STAGE1_APPROVED_CONTENT_SHA256,
)
from sterling.rules.approved import (
    APPROVED_RULES_VERSION_ID as STAGE1_APPROVED_RULES_VERSION_ID,
)
from sterling.rules.approved import (
    DAILY_LOSS_ABSOLUTE_CEILING_GBP,
    DAILY_LOSS_EQUITY_FRACTION,
    PER_TRADE_ABSOLUTE_CEILING_GBP,
    PER_TRADE_EQUITY_FRACTION,
    STARTING_CAPITAL_GBP,
    TOTAL_OPEN_PLANNED_RISK_FRACTION,
)

POLICY_SCHEMA_VERSION = "1"
POLICY_VERSION = "1"
POLICY_VERSION_ID = "stage2-gate1-policy-v1"
POLICY_GATE = "stage2-gate1"

CONCENTRATION_EQUITY_FRACTION = Decimal("0.25")
MAX_SIMULTANEOUS_POSITION_EPISODES = 4
CONSECUTIVE_NET_LOSS_EPISODES = 3

OPERATIONAL_UNAPPROVED = "UNAPPROVED_UNCONFIGURED"
OPERATIONAL_DEFAULT_DECISION = "DENY"

# SHA-256 of the canonical Stage 2 Gate 1 policy with content_hash removed.
POLICY_CONTENT_SHA256 = "3de11ad3298fd0c0aa04cea45639290cc05bb6763d86ca62334057cc0ebe29db"

__all__ = [
    "CONCENTRATION_EQUITY_FRACTION",
    "CONSECUTIVE_NET_LOSS_EPISODES",
    "DAILY_LOSS_ABSOLUTE_CEILING_GBP",
    "DAILY_LOSS_EQUITY_FRACTION",
    "MAX_SIMULTANEOUS_POSITION_EPISODES",
    "OPERATIONAL_DEFAULT_DECISION",
    "OPERATIONAL_UNAPPROVED",
    "PER_TRADE_ABSOLUTE_CEILING_GBP",
    "PER_TRADE_EQUITY_FRACTION",
    "POLICY_CONTENT_SHA256",
    "POLICY_GATE",
    "POLICY_SCHEMA_VERSION",
    "POLICY_VERSION",
    "POLICY_VERSION_ID",
    "STAGE1_APPROVED_CONTENT_SHA256",
    "STAGE1_APPROVED_RULES_VERSION_ID",
    "STARTING_CAPITAL_GBP",
    "TOTAL_OPEN_PLANNED_RISK_FRACTION",
]

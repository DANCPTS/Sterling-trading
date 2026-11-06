"""Owner-approved pins for Desk Rules v0.2.

Absolute ceilings move only when a new rules version is added here and a
matching document is shipped. Raising a ceiling in a file is not enough.
"""

from __future__ import annotations

from decimal import Decimal

APPROVED_RULES_VERSION_ID = "desk-rules-v0.2"
APPROVED_RULES_VERSION = "0.2"
APPROVED_SCHEMA_VERSION = "1"

PER_TRADE_EQUITY_FRACTION = Decimal("0.005")
PER_TRADE_ABSOLUTE_CEILING_GBP = Decimal("2.50")
DAILY_LOSS_EQUITY_FRACTION = Decimal("0.01")
DAILY_LOSS_ABSOLUTE_CEILING_GBP = Decimal("5.00")
TOTAL_OPEN_PLANNED_RISK_FRACTION = Decimal("0.01")
STARTING_CAPITAL_GBP = Decimal("500.00")

# SHA-256 of the canonical v0.2 document with the content_hash field removed.
APPROVED_CONTENT_SHA256 = "d28912d913d9b8d066dca38811c207f921764294dba72c5392aa49523073279c"

"""Shared pilot settings. The research package does not import the xAI client."""

from __future__ import annotations

from decimal import Decimal

# The weekly allocation client. Scripts pass this into the prompt and schema.
CONFIGURED_MODEL = "grok-4.3"

# xAI cost tracking: 1 USD = 10_000_000_000 ticks.
# https://docs.x.ai/developers/cost-tracking
# Stored ticks stay the provider integer. This constant only converts them.
USD_TICKS = 10_000_000_000


def ticks_to_usd(ticks: int) -> Decimal:
    """Provider-reported actual cost. The integer tick count is not modified."""
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        raise ValueError("cost ticks must be a non-negative integer")
    return Decimal(ticks) / Decimal(USD_TICKS)

"""Errors raised by Stage 2 Gate 1 policy loading and direct math calls.

Assessment of a proposal returns findings instead of raising for ordinary
bad inputs. These types are for a policy file that cannot be trusted and
for budget functions called with values that are not decimals.
"""

from __future__ import annotations

from sterling.errors import SterlingError
from sterling.risk.reasons import Reason


class RiskError(SterlingError):
    """Base class for Stage 2 Gate 1 errors."""


class PolicyError(RiskError):
    """The Stage 2 policy document failed a pin, hash, or schema check."""


class RiskInputError(RiskError):
    """A direct budget or fixture call received a value that cannot be used."""

    def __init__(self, reason: Reason, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")

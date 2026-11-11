"""Identifiers shared by Sterling Invest and, later, Sterling FX.

This module is the plug point. It does not fetch a price or send an order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

SYSTEM_INVEST = "sterling-invest"
SYSTEM_FX = "sterling-fx"
EXPERIMENT_V1 = "grok-forward-v1"
EXPERIMENT_V2 = "paper-v2"
EXECUTION_PERMISSION = "NOT_ISSUED"
CADENCE_WEEKLY = "weekly"
CADENCE_INTRADAY = "intraday"


@dataclass(frozen=True, slots=True)
class Experiment:
    """One research system. FX is named here and is not implemented."""

    system: str
    experiment: str
    cadence: str
    base_currency: str = "GBP"

    def log_fields(self) -> dict[str, str]:
        return {
            "base_currency": self.base_currency,
            "cadence": self.cadence,
            "execution_permission": EXECUTION_PERMISSION,
            "experiment": self.experiment,
            "system": self.system,
        }


INVEST_V2 = Experiment(system=SYSTEM_INVEST, experiment=EXPERIMENT_V2, cadence=CADENCE_WEEKLY)


class MarketData(Protocol):
    def price(self, instrument_id: str, session: date, *, kind: str) -> Decimal | None:
        """Return a GBP price for ``kind`` of ``open`` or ``close``, or None."""


class CostSchedule(Protocol):
    def for_session(self, session: date) -> object:
        """Return the dealing-cost assumption for that session."""


class ExecutionRules(Protocol):
    def execution_permission(self) -> str: ...

    def allows_short(self) -> bool: ...

    def allows_leverage(self) -> bool: ...

    def max_gross(self) -> Decimal: ...


class InvestRules:
    """Paper-v2 rules: fully invested at most, no borrow, no short, no order."""

    def execution_permission(self) -> str:
        return EXECUTION_PERMISSION

    def allows_short(self) -> bool:
        return False

    def allows_leverage(self) -> bool:
        return False

    def max_gross(self) -> Decimal:
        return Decimal(1)


def performance_document(
    *,
    experiment: Experiment,
    arms: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """One JSON shape a later dashboard can read for Invest and for FX."""
    return {
        "arms": list(arms),
        "base_currency": experiment.base_currency,
        "cadence": experiment.cadence,
        "execution_permission": EXECUTION_PERMISSION,
        "experiment": experiment.experiment,
        "schema": "sterling-performance-v1",
        "system": experiment.system,
    }

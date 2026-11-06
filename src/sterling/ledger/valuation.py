"""Valuation freshness. This is a label, not permission to trade.

A mark is FRESH only when metrics are asked ``as_of`` a timestamp on the same
Europe/London civil day as the mark, and the mark is already in effect
(``mark_timestamp <= as_of``). Any other open position is STALE, MISSING, or
COST_BASIS. Stage 1 still accounts unmarked positions at cost and still shows
the last mark in equity. Stage 2 must fail closed unless the status it acts on
is FRESH. Calling ``view()`` without ``as_of`` never returns FRESH.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sterling.ledger.calendar import london_date, require_aware
from sterling.ledger.engine import Position, _State

ValuationStatus = Literal["FRESH", "STALE", "MISSING", "COST_BASIS"]


@dataclass(frozen=True, slots=True)
class PositionValuation:
    instrument_id: str
    status: ValuationStatus
    mark_timestamp: datetime | None
    mark_source: str | None


def position_status(position: Position, as_of: datetime | None) -> ValuationStatus:
    """Status of one position. Quantity of zero is MISSING, never FRESH."""
    if position.quantity == 0:
        return "MISSING"
    if position.mark_price is None or position.mark_timestamp is None or position.mark_source is None:
        return "COST_BASIS"
    if as_of is None:
        return "STALE"
    aware = require_aware(as_of)
    if position.mark_timestamp > aware or london_date(position.mark_timestamp) != london_date(aware):
        return "STALE"
    return "FRESH"


def open_valuations(state: _State, as_of: datetime | None) -> tuple[PositionValuation, ...]:
    rows: list[PositionValuation] = []
    for instrument_id in sorted(state.positions):
        position = state.positions[instrument_id]
        if position.quantity == 0:
            continue
        rows.append(
            PositionValuation(
                instrument_id=instrument_id,
                status=position_status(position, as_of),
                mark_timestamp=position.mark_timestamp,
                mark_source=position.mark_source,
            )
        )
    return tuple(rows)


def all_open_positions_fresh(valuations: tuple[PositionValuation, ...]) -> bool:
    """True only when every open position is FRESH.

    An empty book is not fresh. Missing, stale, and cost-basis valuations are
    not permission to trade.
    """
    return bool(valuations) and all(row.status == "FRESH" for row in valuations)

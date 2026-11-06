"""Accounting valuation labels. They are not execution quotes.

``ACCOUNTING_SAME_DAY`` means only that a caller-supplied mark falls on the
same Europe/London civil day as ``as_of`` and is already in effect. It says
nothing about quote age, a future timestamp, source quality, or whether an
order may be sent. Hours-old marks on that day still carry this label.

Stage 1 has no helper that grants permission to trade. Stage 2 must
independently verify maximum quote age, the timestamp (including a rejection
of future timestamps), the source, and validity before approving any order,
and must fail closed otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sterling.ledger.calendar import london_date, require_aware
from sterling.ledger.engine import Position, _State

AccountingValuationStatus = Literal[
    "ACCOUNTING_SAME_DAY",
    "ACCOUNTING_OTHER_DAY",
    "ACCOUNTING_COST_BASIS",
    "ACCOUNTING_NO_OPEN_QUANTITY",
]


@dataclass(frozen=True, slots=True)
class PositionValuation:
    """Accounting classification of one open position. Not an execution quote."""

    instrument_id: str
    valuation_status_for_accounting: AccountingValuationStatus
    mark_timestamp: datetime | None
    mark_source: str | None


def accounting_valuation_status(position: Position, as_of: datetime | None) -> AccountingValuationStatus:
    """Classify a position for the books.

    The result is never an execution-eligible quote. ``ACCOUNTING_SAME_DAY``
    is only a same-civil-day label.
    """
    if position.quantity == 0:
        return "ACCOUNTING_NO_OPEN_QUANTITY"
    if position.mark_price is None or position.mark_timestamp is None or position.mark_source is None:
        return "ACCOUNTING_COST_BASIS"
    if as_of is None:
        return "ACCOUNTING_OTHER_DAY"
    aware = require_aware(as_of)
    if position.mark_timestamp > aware or london_date(position.mark_timestamp) != london_date(aware):
        return "ACCOUNTING_OTHER_DAY"
    return "ACCOUNTING_SAME_DAY"


def open_valuations(state: _State, as_of: datetime | None) -> tuple[PositionValuation, ...]:
    rows: list[PositionValuation] = []
    for instrument_id in sorted(state.positions):
        position = state.positions[instrument_id]
        if position.quantity == 0:
            continue
        rows.append(
            PositionValuation(
                instrument_id=instrument_id,
                valuation_status_for_accounting=accounting_valuation_status(position, as_of),
                mark_timestamp=position.mark_timestamp,
                mark_source=position.mark_source,
            )
        )
    return tuple(rows)

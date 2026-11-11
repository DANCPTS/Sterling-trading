"""Point-in-time Yahoo closes and opens for the daily job.

Prices passed in are already in GBP. The caller converts GBp and GBX by
dividing by 100 before they reach this module. A close is usable only when
its availability time is at or before the valuation. An open is usable only
at or after 08:00 London and at or before the valuation. Nothing here fetches
a network resource or invents a missing print.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sterling.research.invest.schema import FUNDS
from sterling.research.invest.sessions import session_open


def select_market(
    *,
    closes: Sequence[tuple[str, date, Decimal, datetime]],
    opens: Sequence[tuple[str, date, Decimal, datetime]],
    second_sources: Sequence[tuple[str, date, str]],
    session: date,
    as_of: datetime,
) -> tuple[dict[str, Decimal], dict[str, tuple[tuple[date, Decimal], ...]], dict[str, str]]:
    """Return opens, close history, and a cross-check note per fund."""
    history: dict[str, list[tuple[date, Decimal]]] = {name: [] for name in FUNDS}
    for ticker, day, price, available in closes:
        if ticker in history and available <= as_of:
            history[ticker].append((day, price))
    live_opens: dict[str, Decimal] = {}
    opened = session_open(session)
    for ticker, day, price, available in opens:
        if ticker not in history or day != session or available > as_of:
            continue
        if opened is None or available < opened:
            continue
        live_opens[ticker] = price
    checked = {name: False for name in FUNDS}
    sources = {name: "" for name in FUNDS}
    for ticker, day, source in second_sources:
        if ticker in checked and day == session:
            checked[ticker] = True
            sources[ticker] = source
    notes = {}
    for name in FUNDS:
        if checked[name]:
            notes[name] = f"cross-checked against {sources[name]}"
        else:
            notes[name] = "yahoo-chart only; no second source for this session"
    return live_opens, {name: tuple(points) for name, points in history.items()}, notes

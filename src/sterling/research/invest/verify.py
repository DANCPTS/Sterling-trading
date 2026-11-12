"""Reject a paper-v2 log that mixes versions or lanes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from sterling.research.errors import ResearchError
from sterling.research.grok.verify import verify_evidence_lane
from sterling.research.invest.identity import EXPERIMENT_V2, SYSTEM_INVEST


def verify_invest(records: Sequence[Mapping[str, object]], *, lane: str) -> None:
    verify_evidence_lane(records, lane=lane)
    for record in records:
        if record.get("system") != SYSTEM_INVEST or record.get("experiment") != EXPERIMENT_V2:
            raise ResearchError("paper-v2 log contains a record from another experiment")
        permission = record.get("execution_permission")
        if permission is not None and permission != "NOT_ISSUED":
            raise ResearchError("paper-v2 record issued execution permission")
    _unique(records, "valuation")
    _unique(records, "cash_interest")
    _unique(records, "interest_pending")
    _unique_fills(records)


def _unique(records: Sequence[Mapping[str, object]], kind: str) -> None:
    seen: set[str] = set()
    for record in records:
        if record.get("record_type") != kind:
            continue
        session = record.get("session")
        if not isinstance(session, str) or session in seen:
            raise ResearchError(f"duplicate {kind} for one session")
        seen.add(session)


def _unique_fills(records: Sequence[Mapping[str, object]]) -> None:
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        kind = record.get("record_type")
        if kind not in {"fill", "fill_skipped"}:
            continue
        key = (str(record.get("session")), str(record.get("fund")), str(kind))
        if key in seen:
            raise ResearchError("duplicate fill for one fund on one session")
        seen.add(key)


def ledger_warnings(records: Sequence[Mapping[str, object]]) -> list[str]:
    """Point out stored figures that disagree. This does not reject the log.

    An older paper log can verify and still carry a cumulative return measured
    from the first mark instead of the starting capital. The page shows that
    disagreement. The log itself is left unchanged.
    """
    notes: list[str] = []
    start = _start_cash(records)
    valuations = [record for record in records if record.get("record_type") == "valuation"]
    if valuations and start is not None and start > 0:
        nav = _amount(valuations[-1].get("nav_gbp"))
        stored = _amount(valuations[-1].get("cumulative_return"))
        if nav is not None and stored is not None:
            implied = nav / start - 1
            if abs(stored - implied) > Decimal("0.0000001"):
                notes.append(
                    "Verified log cumulative return "
                    f"{_percent(stored)} does not match NAV against starting capital ({_percent(implied)})."
                )
    previous = start
    high: Decimal | None = start
    worst = Decimal(0)
    total = Decimal(0)
    last_nav: Decimal | None = None
    for record in valuations:
        session = str(record.get("session"))
        row_nav = _amount(record.get("nav_gbp"))
        daily = _amount(record.get("daily_pnl_gbp"))
        if row_nav is not None and previous is not None and daily is not None:
            if abs((row_nav - previous) - daily) > Decimal("0.02"):
                notes.append(f"Day P&L on {session} does not match the change in NAV.")
            total += daily
        marked = _marked(record)
        if row_nav is not None and marked is not None and abs(marked - row_nav) > Decimal("0.05"):
            notes.append(f"Cash plus marked holdings on {session} does not match NAV.")
        water = _amount(record.get("high_water_gbp"))
        drawdown = _amount(record.get("drawdown"))
        if water is not None and row_nav is not None and drawdown is not None and water > 0:
            current = (water - row_nav) / water if row_nav < water else Decimal(0)
            if drawdown + Decimal("0.0000001") < current:
                notes.append(f"Drawdown on {session} is below the decline from the recorded high water.")
            if high is not None and water + Decimal("0.01") < high:
                notes.append(f"High water on {session} fell.")
            if drawdown + Decimal("0.0000001") < worst:
                notes.append(f"Drawdown on {session} fell below the earlier maximum.")
            high = water
            worst = drawdown
        if row_nav is not None:
            previous = row_nav
            last_nav = row_nav
    if start is not None and last_nav is not None and abs(total - (last_nav - start)) > Decimal("0.05"):
        notes.append("The sum of day P&L does not equal cumulative P&L.")
    return notes


def _start_cash(records: Sequence[Mapping[str, object]]) -> Decimal | None:
    for record in records:
        if record.get("record_type") == "decision":
            return _amount(record.get("cash_gbp"))
    return None


def _marked(record: Mapping[str, object]) -> Decimal | None:
    cash = _amount(record.get("cash_gbp"))
    holdings = record.get("holdings")
    prices = record.get("prices")
    if cash is None or not isinstance(holdings, dict) or not isinstance(prices, dict):
        return None
    total = cash
    for name, units in holdings.items():
        info = prices.get(str(name))
        price = _amount(info.get("price_gbp")) if isinstance(info, dict) else None
        quantity = _amount(units)
        if price is None or quantity is None:
            return None
        total += quantity * price
    return total


def _amount(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _percent(value: Decimal) -> str:
    return f"{value * 100:.2f}%"


def verify_v1_closed(records: Sequence[Mapping[str, object]]) -> None:
    """The original forward log stays v1. A paper-v2 line does not belong in it."""
    for record in records:
        if record.get("experiment") not in (None, "grok-forward-v1"):
            raise ResearchError("v1 log contains another experiment")
        if record.get("system") not in (None,):
            raise ResearchError("v1 log contains another system")

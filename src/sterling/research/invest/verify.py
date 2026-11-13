"""Reject a paper-v2 log that mixes versions or lanes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from sterling.research.errors import ResearchError
from sterling.research.grok.verify import verify_evidence_lane
from sterling.research.invest.identity import EXPERIMENT_V2, SYSTEM_INVEST
from sterling.research.invest.registration import STARTING_CASH_GBP


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
    notes.extend(_drawdown_notes(records))
    notes.extend(_benchmark_notes(records))
    return notes


def programme_start(records: Sequence[Mapping[str, object]]) -> Decimal:
    """Cash recorded on the decision, otherwise the registered £10,000."""
    for record in records:
        if record.get("record_type") == "decision":
            amount = _amount(record.get("cash_gbp"))
            if amount is not None and amount > 0:
                return amount
    return STARTING_CASH_GBP


def observable_closes(records: Sequence[Mapping[str, object]]) -> list[Decimal]:
    """Recorded valuation NAVs, in log order. Provisional marks are not included."""
    closes: list[Decimal] = []
    for record in records:
        if record.get("record_type") != "valuation":
            continue
        nav = _amount(record.get("nav_gbp"))
        if nav is not None:
            closes.append(nav)
    return closes


def closing_drawdown(start: Decimal, closes: Sequence[Decimal]) -> Decimal:
    """Max decline from daily closes, including the starting capital.

    This is not an audited intraday drawdown. A high water that was never a
    recorded close does not enter the path.
    """
    peak = start
    worst = Decimal(0)
    for nav in closes:
        if nav > peak:
            peak = nav
        if peak > 0 and nav < peak:
            gap = (peak - nav) / peak
            if gap > worst:
                worst = gap
    return worst


def same_start_return(end: Decimal, start: Decimal) -> Decimal:
    if start <= 0:
        return Decimal(0)
    return end / start - 1


def _drawdown_notes(records: Sequence[Mapping[str, object]]) -> list[str]:
    closes = observable_closes(records)
    valuations = [record for record in records if record.get("record_type") == "valuation"]
    if not valuations or not closes:
        return []
    latest = valuations[-1]
    session = str(latest.get("session") or "the latest valuation")
    start = programme_start(records)
    reconstructed = closing_drawdown(start, closes)
    stored = _amount(latest.get("drawdown"))
    water = _amount(latest.get("high_water_gbp"))
    if stored is None:
        return [
            f"Stored drawdown on {session} cannot be read. "
            f"Daily-close drawdown from starting capital {_gbp_text(start)} and the recorded closing NAVs "
            f"is {_precise(reconstructed)} (shown as {_percent(reconstructed)}). "
            "That figure is the decline between daily closes. It is not an audited intraday drawdown."
        ]
    daily_peak = max([start, *closes])
    water_above = water is not None and water > daily_peak + Decimal("0.01")
    if abs(stored - reconstructed) <= Decimal("0.0000001") and not water_above:
        return []
    water_text = _gbp_text(water) if water is not None else "unreadable"
    return [
        f"Stored drawdown {_percent(stored)} on {session} uses high water {water_text}. "
        f"Daily-close drawdown from starting capital {_gbp_text(start)} and the recorded closing NAVs "
        f"is {_precise(reconstructed)} (shown as {_percent(reconstructed)}). "
        "That figure is the decline between daily closes. It is not an audited intraday drawdown."
    ]


def _benchmark_notes(records: Sequence[Mapping[str, object]]) -> list[str]:
    notes: list[str] = []
    performance = [record for record in records if record.get("record_type") == "performance"]
    valuations = [record for record in records if record.get("record_type") == "valuation"]
    if performance and valuations:
        older = str(performance[-1].get("session") or "")
        newer = str(valuations[-1].get("session") or "")
        if older and newer and older < newer:
            notes.append(f"Benchmark row {older} is older than the latest valuation {newer}. It is not a new mark.")
    if not performance:
        return notes
    start = programme_start(records)
    raw = performance[-1].get("arms")
    arms = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    for arm in arms:
        name = str(arm.get("name") or "").strip()
        if not name:
            notes.append("A benchmark arm has no name, so its return was not reconstructed.")
            continue
        end = _amount(arm.get("end_nav_gbp"))
        if end is None:
            notes.append(f"{name} has no readable end value, so its return was not reconstructed.")
            continue
        reconstructed = same_start_return(end, start)
        stored = _amount(arm.get("cumulative_return"))
        path = closing_drawdown(start, _arm_closes(records, name))
        stored_dd = _amount(arm.get("max_drawdown"))
        return_off = stored is None or abs(stored - reconstructed) > Decimal("0.0000001")
        drawdown_off = stored_dd is None or abs(stored_dd - path) > Decimal("0.0000001")
        if not return_off and not drawdown_off:
            continue
        stored_return = _percent(stored) if stored is not None else "unreadable"
        stored_draw = _percent(stored_dd) if stored_dd is not None else "unreadable"
        notes.append(
            f"{name} stored return {stored_return} and stored max drawdown {stored_draw}. "
            f"From starting capital {_gbp_text(start)} the same-start return is {_signed_precise(reconstructed)} "
            f"(shown as {_signed_percent(reconstructed)}) and the daily-close drawdown from the recorded "
            f"daily marks is {_precise(path)} (shown as {_percent(path)}). "
            "The daily-close figure is not an audited intraday drawdown."
        )
    return notes


def _arm_closes(records: Sequence[Mapping[str, object]], name: str) -> list[Decimal]:
    closes: list[Decimal] = []
    for record in records:
        if record.get("record_type") != "performance":
            continue
        raw = record.get("arms")
        if not isinstance(raw, list):
            continue
        for arm in raw:
            if isinstance(arm, dict) and arm.get("name") == name:
                end = _amount(arm.get("end_nav_gbp"))
                if end is not None:
                    closes.append(end)
    return closes


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


def _precise(value: Decimal) -> str:
    return f"{value * 100:.4f}%"


def _signed_percent(value: Decimal) -> str:
    text = _percent(value)
    if value > 0:
        return f"+{text}"
    return text


def _signed_precise(value: Decimal) -> str:
    text = _precise(value)
    if value > 0:
        return f"+{text}"
    return text


def _gbp_text(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}£{abs(value):,.2f}"


def verify_v1_closed(records: Sequence[Mapping[str, object]]) -> None:
    """The original forward log stays v1. A paper-v2 line does not belong in it."""
    for record in records:
        if record.get("experiment") not in (None, "grok-forward-v1"):
            raise ResearchError("v1 log contains another experiment")
        if record.get("system") not in (None,):
            raise ResearchError("v1 log contains another system")

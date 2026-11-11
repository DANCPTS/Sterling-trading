"""Daily paper settlement. No model is called.

Opens are the caller's genuine next-session opening prices. A missing open
stays pending and is recorded as ``fill_skipped``. A close is never used as a
silent substitute for that open. Yahoo's daily chart open is the live source
(``yahoo-open``). Synthetic demos must label their own source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.invest.benchmarks import benchmark_table
from sterling.research.invest.book import Book, accrue_cash, mark, trade_toward
from sterling.research.invest.cash_rate import HAIRCUT_PERCENTAGE_POINTS, cash_quote, unavailable_rate
from sterling.research.invest.schema import FUNDS
from sterling.research.invest.sessions import first_session_after, session_open
from sterling.research.money import ZERO, quantize_money

OPEN_SOURCE_LIVE = "yahoo-open"
STALE_SESSIONS = 5


def load_book(log_path: Path) -> Book:
    return _replay(ProposalLog(log_path).records())[0]


def run_daily(
    *,
    log_path: Path,
    session: date,
    opens: Mapping[str, Decimal],
    close_history: Mapping[str, Sequence[tuple[date, Decimal]]],
    sonia: Sequence[tuple[date, Decimal]],
    recorded_at: datetime,
    open_source: str,
    data_label: str,
    stop_after: str | None = None,
    cross_check: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Settle one London session. A second call for that date appends nothing."""
    if "xai" in open_source.lower() or "provider" in open_source.lower():
        raise RuntimeError("daily settlement does not call a provider")
    log = _open(log_path, recorded_at)
    records = log.records()
    if _has(records, "valuation", session) and _has(records, "performance", session):
        return {"status": "noop", "session": session.isoformat(), "provider_calls": 0}
    book, interest_days, filled, skipped_notes = _replay(records)
    moment = recorded_at
    quote = cash_quote(sonia, session, as_of=recorded_at)
    for day in _interest_due(session, interest_days):
        day_quote = cash_quote(sonia, day, as_of=recorded_at)
        if day_quote is None:
            continue
        rate = day_quote.annual_decimal
        amount = accrue_cash(book, rate)
        fields = {
            "amount_gbp": format(amount, "f"),
            "annual_decimal": format(rate, "f"),
            "haircut_percentage_points": format(HAIRCUT_PERCENTAGE_POINTS, "f"),
            "published_on": "" if day_quote is None else day_quote.published_on.isoformat(),
            "series": "IUDSOIA",
            "session": day.isoformat(),
            "sonia_percent": "" if day_quote is None else format(day_quote.sonia_percent, "f"),
            "source": "unavailable" if day_quote is None else day_quote.source,
        }
        moment = _append(log, moment, "cash_interest", fields)
    if stop_after == "interest":
        return {"provider_calls": 0, "session": session.isoformat(), "status": "stopped", "nav_gbp": format(book.cash, "f")}
    pending = book.pending or {}
    prices, statuses = _marks(close_history, session, opens, cross_check or {})
    skipped: list[str] = []
    eligible = first_session_after(book.decided_at) if book.decided_at is not None else None
    opened = session_open(session)
    for ticker in FUNDS:
        if ticker not in pending:
            continue
        key = (session.isoformat(), ticker)
        if key in filled:
            continue
        reason = _fill_block(session, eligible, opened, recorded_at, ticker in opens)
        if reason is not None:
            skipped.append(ticker)
            if key not in skipped_notes:
                moment = _append(
                    log,
                    moment,
                    "fill_skipped",
                    {"fund": ticker, "reason": reason, "session": session.isoformat(), "source": open_source},
                )
            continue
        if not _can_size(book, prices):
            skipped.append(ticker)
            if key not in skipped_notes:
                moment = _append(
                    log,
                    moment,
                    "fill_skipped",
                    {
                        "fund": ticker,
                        "reason": "valuation_incomplete",
                        "session": session.isoformat(),
                        "source": open_source,
                    },
                )
            continue
        for fill in trade_toward(book, ticker, pending[ticker], opens[ticker], prices, session):
            fill["source"] = open_source
            fill["open_at"] = opened.isoformat() if opened is not None else ""
            moment = _append(log, moment, "fill", fill)
    if stop_after == "fills":
        return {"provider_calls": 0, "session": session.isoformat(), "status": "stopped", "nav_gbp": format(book.cash, "f")}
    if _can_size(book, prices):
        marked = mark(book, prices, session)
        nav = marked[0] if marked is not None else book.cash
    else:
        nav = book.cash
    if not _has(log.records(), "valuation", session):
        moment = _append(log, moment, "valuation", _valuation(book, session, nav, statuses, skipped, quote, data_label))
    if not _has(log.records(), "performance", session):
        start = session - timedelta(days=40)
        sessions = [day for day in _sessions_in(log.records(), start, session)]
        visible = {name: tuple((day, price) for day, price in points if day <= session) for name, points in close_history.items()}
        table = benchmark_table(visible, sessions or [session], sonia=sonia)
        table["performance_schema"] = table.pop("schema")
        table["data_label"] = data_label
        table["session"] = session.isoformat()
        _append(log, moment, "performance", table)
    return {"provider_calls": 0, "session": session.isoformat(), "status": "marked", "nav_gbp": format(nav, "f")}


def _marks(
    history: Mapping[str, Sequence[tuple[date, Decimal]]],
    session: date,
    opens: Mapping[str, Decimal],
    cross_check: Mapping[str, str],
) -> tuple[dict[str, Decimal], dict[str, dict[str, object]]]:
    prices: dict[str, Decimal] = {}
    statuses: dict[str, dict[str, object]] = {}
    for ticker in FUNDS:
        points = [item for item in history.get(ticker, ()) if item[0] <= session]
        fresh = next((price for day, price in points if day == session), None)
        prior = [(day, price) for day, price in points if day < session]
        if fresh is not None:
            prices[ticker] = fresh
            statuses[ticker] = {
                "age_sessions": 0,
                "cross_check": cross_check.get(ticker, "not assessed"),
                "cross_checked": "yes" if str(cross_check.get(ticker, "")).startswith("cross-checked") else "no",
                "last_good": session.isoformat(),
                "price_gbp": format(fresh, "f"),
                "status": "fresh",
            }
            continue
        if prior:
            last_day, last_price = max(prior)
            age = _weekdays(last_day, session)
            statuses[ticker] = {
                "age_sessions": age,
                "cross_check": cross_check.get(ticker, "not assessed"),
                "cross_checked": "yes" if str(cross_check.get(ticker, "")).startswith("cross-checked") else "no",
                "last_good": last_day.isoformat(),
                "price_gbp": format(last_price, "f"),
                "status": "stale" if age <= STALE_SESSIONS else "missing",
            }
            if age <= STALE_SESSIONS:
                prices[ticker] = last_price
            continue
        statuses[ticker] = {
            "age_sessions": None,
            "cross_check": cross_check.get(ticker, "not assessed"),
            "cross_checked": "no",
            "last_good": None,
            "price_gbp": None,
            "status": "missing",
        }
    for ticker, price in opens.items():
        if ticker not in prices:
            prices[ticker] = price
    return prices, statuses


def _can_size(book: Book, prices: Mapping[str, Decimal]) -> bool:
    for ticker in FUNDS:
        if book.quantity(ticker) > 0 and ticker not in prices:
            return False
    return True


def _fill_block(
    session: date,
    eligible: date | None,
    opened: datetime | None,
    recorded_at: datetime,
    has_open: bool,
) -> str | None:
    """None means this session's open may fill. Any other string is a skip reason."""
    if eligible is None:
        return "decision_time_missing"
    if opened is None or session < eligible:
        return "open_not_after_decision"
    if session > eligible:
        return "not_first_open_after_decision"
    if opened > recorded_at or not has_open:
        return "opening_price_unavailable"
    return None


def _replay(
    records: Sequence[Mapping[str, object]],
) -> tuple[Book, set[str], set[tuple[str, str]], set[tuple[str, str]]]:
    book = Book()
    interest: set[str] = set()
    filled: set[tuple[str, str]] = set()
    skipped: set[tuple[str, str]] = set()
    pending: dict[str, Decimal] | None = None
    for record in records:
        kind = record.get("record_type")
        if kind == "decision" and record.get("gate") == "accepted":
            raw = record.get("accepted_weights")
            pending = {}
            if isinstance(raw, dict):
                pending = {str(name): Decimal(str(value)) for name, value in raw.items()}
            filled = set()
            skipped = set()
            book.pending = pending
            book.decided_at = _decision_time(record)
        elif kind == "fill":
            _apply_fill(book, record)
            filled.add((str(record.get("session")), str(record.get("fund"))))
        elif kind == "fill_skipped":
            skipped.add((str(record.get("session")), str(record.get("fund"))))
        elif kind == "cash_interest":
            book.cash = quantize_money(book.cash + Decimal(str(record.get("amount_gbp"))))
            interest.add(str(record.get("session")))
        elif kind == "valuation":
            nav_text = record.get("nav_gbp")
            day_text = record.get("session")
            if isinstance(nav_text, str) and isinstance(day_text, str):
                book.nav_rows.append((date.fromisoformat(day_text), Decimal(nav_text)))
    peak = START_NAV
    worst = ZERO
    for _day, nav in book.nav_rows:
        if nav > peak:
            peak = nav
        if peak > 0 and (peak - nav) / peak > worst:
            worst = (peak - nav) / peak
    book.peak = peak
    book.max_drawdown = worst
    book.pending = pending
    return book, interest, filled, skipped


def _decision_time(record: Mapping[str, object]) -> datetime | None:
    for key in ("timestamp_utc", "recorded_at"):
        value = record.get(key)
        if isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed
    return None


def _apply_fill(book: Book, record: Mapping[str, object]) -> None:
    fund = str(record.get("fund"))
    units = Decimal(str(record.get("units")))
    delta = Decimal(str(record.get("cash_delta_gbp")))
    book.cash = quantize_money(book.cash + delta)
    signed = units if record.get("side") == "BUY" else -units
    book.quantities[fund] = book.quantity(fund) + signed
    book.trades += 1
    for key in ("spread_gbp", "slippage_gbp", "stamp_gbp", "commission_gbp", "fx_cost_gbp"):
        book.cumulative_costs = quantize_money(book.cumulative_costs + Decimal(str(record.get(key) or "0")))


def _valuation(
    book: Book,
    session: date,
    nav: Decimal,
    statuses: Mapping[str, Mapping[str, object]],
    skipped: list[str],
    quote: object,
    data_label: str,
) -> dict[str, object]:
    history = book.nav_rows[:-1]
    start = history[0][1] if history else START_NAV
    previous = history[-1][1] if history else START_NAV
    cumulative = (nav / start - 1) if start > 0 else ZERO
    period = (nav / previous - 1) if previous > 0 else ZERO
    daily_pnl = nav - previous
    rate_fields = quote.as_log() if hasattr(quote, "as_log") else unavailable_rate()
    return {
        "cash_gbp": format(book.cash, "f"),
        "cash_rate": rate_fields,
        "cumulative_costs_gbp": format(book.cumulative_costs, "f"),
        "cumulative_return": format(cumulative, "f"),
        "daily_pnl_gbp": format(daily_pnl, "f"),
        "data_label": data_label,
        "drawdown": format(book.max_drawdown, "f"),
        "high_water_gbp": format(book.peak, "f"),
        "holdings": {name: format(book.quantity(name), "f") for name in FUNDS if book.quantity(name) != 0},
        "nav_gbp": format(nav, "f"),
        "pending_no_open": skipped,
        "period_return": format(period, "f"),
        "prices": {name: dict(statuses[name]) for name in FUNDS},
        "session": session.isoformat(),
    }


START_NAV = Decimal("10000.00")


def _sessions_in(records: Sequence[Mapping[str, object]], start: date, end: date) -> list[date]:
    found = {end}
    for record in records:
        text = record.get("session")
        if isinstance(text, str):
            day = date.fromisoformat(text)
            if start <= day <= end:
                found.add(day)
    return sorted(found)


def _has(records: Sequence[Mapping[str, object]], kind: str, session: date) -> bool:
    token = session.isoformat()
    return any(record.get("record_type") == kind and record.get("session") == token for record in records)


def _interest_due(session: date, already: set[str]) -> list[date]:
    """Calendar days since the previous interest record, including this session."""
    if not already:
        return [session]
    last = max(date.fromisoformat(item) for item in already)
    days: list[date] = []
    cursor = last + timedelta(days=1)
    while cursor <= session:
        if cursor.isoformat() not in already:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _weekdays(start: date, end: date) -> int:
    count = 0
    day = start
    while day < end:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def _open(path: Path, when: datetime) -> ProposalLog:
    log = ProposalLog(path)
    if not path.exists():
        log.create(now=when - timedelta(seconds=1))
    return log


def _append(log: ProposalLog, when: datetime, record_type: str, fields: Mapping[str, object]) -> datetime:
    previous = datetime.fromisoformat(str(log.records()[-1]["timestamp_utc"]))
    stamped = when if when > previous else previous + timedelta(microseconds=1)
    log.append_event(timestamp_utc=stamped, record_type=record_type, fields=fields)
    return stamped

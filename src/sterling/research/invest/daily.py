"""Daily paper settlement. No model is called.

Opens are the caller's genuine next-session opening prices. A fill is allowed
only at an XLON 08:00 Europe/London open strictly after the decision. Weekends,
England and Wales bank holidays, and British Summer Time come from that
calendar. A missing open stays pending (``fill_skipped`` /
``opening_price_unavailable``) and fills at the next later open that is
published. It never fills on an earlier session. If it is still unfilled at
the fifth XLON session, or a newer weekly decision replaces it, the order is
logged ``fill_skipped`` with reason ``EXPIRED`` and is not filled afterwards.
A close is never used as a silent substitute for an open. Yahoo's daily chart
open is the live source (``yahoo-open``). Synthetic demos must label their
own source.

Cash interest accrues on every calendar day, including weekends, at the latest
IUDSOIA print whose reference date is on or before that day and whose
publication time (09:00 London on the next XLON session) is at or before the
job clock. That is the same rate rule the benchmarks use. A day with no
published print is ``interest_pending`` (no amount, not zero) and is retried
later, including a gap under a newer accrual. ``accrued_through`` is the last
calendar day of the contiguous accrued prefix. A print that was not published
by the job's clock is not used.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sterling.research.calendar import LONDON
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.invest.benchmarks import benchmark_table
from sterling.research.invest.book import Book, accrue_cash, mark, trade_toward
from sterling.research.invest.cash_rate import (
    HAIRCUT_PERCENTAGE_POINTS,
    SONIA_SOURCE,
    cash_quote,
    unavailable_rate,
)
from sterling.research.invest.schema import FUNDS
from sterling.research.invest.sessions import first_session_after, session_open
from sterling.research.money import ZERO, quantize_money

OPEN_SOURCE_LIVE = "yahoo-open"
STALE_SESSIONS = 5
# The first eligible XLON session counts as one. The fifth is the last chance.
FILL_EXPIRY_SESSIONS = 5


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
    accrued, pending_days = _interest_sets(records)
    origin = _interest_origin(session, records, accrued)
    credited: dict[date, Decimal] = {}
    for day in _interest_due(session, accrued, origin):
        day_quote = cash_quote(sonia, day, as_of=recorded_at)
        if day_quote is None:
            if day.isoformat() not in pending_days:
                moment = _append(log, moment, "interest_pending", _pending_fields(day, origin, accrued))
                pending_days.add(day.isoformat())
            continue
        rate = day_quote.annual_decimal
        base = _cash_before(records, day) + sum(value for prior, value in credited.items() if prior < day)
        amount = _credit_interest(book, base, rate)
        credited[day] = amount
        accrued.add(day.isoformat())
        book.accrued_through = _accrued_through(origin, accrued)
        moment = _append(
            log,
            moment,
            "cash_interest",
            {
                "accrued_through": "" if book.accrued_through is None else book.accrued_through.isoformat(),
                "amount_gbp": format(amount, "f"),
                "annual_decimal": format(rate, "f"),
                "haircut_percentage_points": format(HAIRCUT_PERCENTAGE_POINTS, "f"),
                "published_on": day_quote.published_on.isoformat(),
                "series": "IUDSOIA",
                "session": day.isoformat(),
                "sonia_percent": format(day_quote.sonia_percent, "f"),
                "source": day_quote.source,
                "status": "accrued",
            },
        )
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
        if key in filled or _expired(ticker, skipped_notes):
            continue
        offset = _session_count(eligible, session)
        reason: str | None
        if offset is not None and (offset > FILL_EXPIRY_SESSIONS or (offset == FILL_EXPIRY_SESSIONS and ticker not in opens)):
            reason = "EXPIRED"
        else:
            reason = _fill_block(
                session,
                eligible,
                opened,
                recorded_at,
                ticker in opens,
                _earlier_opens_missing(eligible, session, ticker, skipped_notes),
            )
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
        table = benchmark_table(visible, sessions or [session], sonia=sonia, as_of=recorded_at)
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


def _credit_interest(book: Book, base: Decimal, annual_rate: Decimal) -> Decimal:
    """Credit one day on ``base``. A gap uses the cash from before later accruals."""
    if base == book.cash:
        return accrue_cash(book, annual_rate)
    rate = annual_rate if annual_rate > 0 else ZERO
    amount = quantize_money(base * rate / Decimal(365))
    book.cash = quantize_money(book.cash + amount)
    book.interest = quantize_money(book.interest + amount)
    return amount


def _cash_before(records: Sequence[Mapping[str, object]], day: date) -> Decimal:
    """Cash at the interest step of ``day``: earlier fills and interest only."""
    cash = START_NAV
    for record in records:
        kind = record.get("record_type")
        text = record.get("session")
        if not isinstance(text, str) or date.fromisoformat(text) >= day:
            continue
        if kind == "cash_interest":
            cash += Decimal(str(record.get("amount_gbp")))
        elif kind == "fill":
            cash += Decimal(str(record.get("cash_delta_gbp")))
    return quantize_money(cash)


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
    earlier_opens_missing: bool,
) -> str | None:
    """None means this session's open may fill. Any other string is a skip reason."""
    if eligible is None:
        return "decision_time_missing"
    if opened is None or session < eligible:
        return "open_not_after_decision"
    if session > eligible and not earlier_opens_missing:
        return "not_first_open_after_decision"
    if opened > recorded_at or not has_open:
        return "opening_price_unavailable"
    return None


def _earlier_opens_missing(
    eligible: date | None,
    session: date,
    ticker: str,
    skipped: Mapping[tuple[str, str], str],
) -> bool:
    """True when every session from the first eligible day up to this one lacked an open."""
    if eligible is None or session <= eligible:
        return False
    day = eligible
    while day < session:
        if session_open(day) is not None and skipped.get((day.isoformat(), ticker)) != "opening_price_unavailable":
            return False
        day += timedelta(days=1)
    return True


def _replay(
    records: Sequence[Mapping[str, object]],
) -> tuple[Book, set[str], set[tuple[str, str]], dict[tuple[str, str], str]]:
    book = Book()
    interest: set[str] = set()
    filled: set[tuple[str, str]] = set()
    skipped: dict[tuple[str, str], str] = {}
    pending: dict[str, Decimal] | None = None
    for record in records:
        kind = record.get("record_type")
        if kind == "decision" and record.get("gate") == "accepted":
            raw = record.get("accepted_weights")
            pending = {}
            if isinstance(raw, dict):
                pending = {str(name): Decimal(str(value)) for name, value in raw.items()}
            filled = set()
            skipped = {}
            book.pending = pending
            book.decided_at = _decision_time(record)
        elif kind == "fill":
            _apply_fill(book, record)
            filled.add((str(record.get("session")), str(record.get("fund"))))
        elif kind == "fill_skipped":
            skipped[(str(record.get("session")), str(record.get("fund")))] = str(record.get("reason"))
        elif kind == "cash_interest" and str(record.get("status") or "accrued") != "pending":
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
    origin = _interest_origin(date.max, records, interest)
    if origin != date.max:
        book.accrued_through = _accrued_through(origin, interest)
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
    # Cumulative return is against the £10,000 starting capital. The first
    # recorded mark is already after costs, so it is not the baseline.
    start = START_NAV
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


def expire_replaced(log: ProposalLog, book: Book, when: datetime) -> None:
    """Log EXPIRED for a resting missing-open order before a new decision replaces it."""
    if not book.pending:
        return
    filled: set[str] = set()
    expired: set[str] = set()
    resting: set[str] = set()
    for record in log.records():
        fund = str(record.get("fund"))
        if record.get("record_type") == "fill":
            filled.add(fund)
        elif record.get("record_type") == "fill_skipped" and record.get("reason") == "EXPIRED":
            expired.add(fund)
        elif record.get("record_type") == "fill_skipped" and record.get("reason") == "opening_price_unavailable":
            resting.add(fund)
    session = when.astimezone(LONDON).date().isoformat()
    for ticker in FUNDS:
        if ticker not in book.pending or ticker in filled or ticker in expired or ticker not in resting:
            continue
        _append(
            log,
            when,
            "fill_skipped",
            {"fund": ticker, "reason": "EXPIRED", "session": session, "source": "next_decision"},
        )


def _expired(ticker: str, skipped: Mapping[tuple[str, str], str]) -> bool:
    return any(fund == ticker and reason == "EXPIRED" for (_day, fund), reason in skipped.items())


def _session_count(eligible: date | None, session: date) -> int | None:
    """1-based XLON sessions from the first eligible day through ``session``."""
    if eligible is None or session < eligible or session_open(session) is None:
        return None
    count = 0
    day = eligible
    while day <= session:
        if session_open(day) is not None:
            count += 1
        day += timedelta(days=1)
    return count


def _interest_sets(records: Sequence[Mapping[str, object]]) -> tuple[set[str], set[str]]:
    accrued: set[str] = set()
    pending: set[str] = set()
    for record in records:
        session = record.get("session")
        if not isinstance(session, str):
            continue
        kind = record.get("record_type")
        if kind == "cash_interest" and str(record.get("status") or "accrued") != "pending":
            accrued.add(session)
        elif kind == "interest_pending":
            pending.add(session)
    return accrued, pending


def _interest_origin(session: date, records: Sequence[Mapping[str, object]], accrued: set[str]) -> date:
    origin = session
    for record in records:
        text = record.get("session")
        if isinstance(text, str):
            day = date.fromisoformat(text)
            if day < origin:
                origin = day
    for item in accrued:
        day = date.fromisoformat(item)
        if day < origin:
            origin = day
    return origin


def _accrued_through(origin: date, accrued: set[str]) -> date | None:
    """Last day of the contiguous accrued prefix. A hole stops the prefix."""
    cursor = origin
    last: date | None = None
    while cursor.isoformat() in accrued:
        last = cursor
        cursor += timedelta(days=1)
    return last


def _pending_fields(day: date, origin: date, accrued: set[str]) -> dict[str, str]:
    through = _accrued_through(origin, accrued)
    return {
        "accrued_through": "" if through is None else through.isoformat(),
        "reason": "sonia_not_yet_published",
        "series": "IUDSOIA",
        "session": day.isoformat(),
        "source": SONIA_SOURCE,
        "status": "pending",
    }


def _interest_due(session: date, accrued: set[str], origin: date) -> list[date]:
    """Every calendar day from the ledger origin through ``session`` that has not accrued."""
    days: list[date] = []
    cursor = origin
    while cursor <= session:
        if cursor.isoformat() not in accrued:
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

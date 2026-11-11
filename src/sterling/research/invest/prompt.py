"""Point-in-time prompt for a paper-v2 allocation. The model is not asked for hashes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal

from sterling.research.invest.book import Book, weights_of
from sterling.research.invest.registration import NO_TRADE_BAND, STARTING_CASH_GBP, SUM_TOLERANCE
from sterling.research.invest.schema import ALLOCATION_SCHEMA, CASH, FUNDS
from sterling.research.money import ZERO, quantize_money

Print = tuple[str, date, Decimal, datetime]
STALE_SESSIONS = 5


def allocation_prompt(
    *,
    book: Book,
    prints: Sequence[Print],
    as_of: datetime,
    tradable: set[str],
) -> str:
    """JSON the weekly job sends. Prints available after ``as_of`` are omitted."""
    visible = [item for item in prints if item[0] in FUNDS and item[3] <= as_of]
    latest: dict[str, Print] = {}
    for item in visible:
        current = latest.get(item[0])
        if current is None or item[1] >= current[1]:
            latest[item[0]] = item
    prices = {ticker: item[2] for ticker, item in latest.items()}
    weights = weights_of(book, prices, as_of.date()) or {name: ZERO for name in (*FUNDS, CASH)}
    nav = _nav(book, prices)
    holdings = []
    for ticker in FUNDS:
        quote = latest.get(ticker)
        quantity = book.quantity(ticker)
        if quote is None:
            holdings.append(
                {
                    "as_of": None,
                    "instrument_id": ticker,
                    "price_gbp": None,
                    "session": None,
                    "staleness": "missing",
                    "tradable": ticker in tradable,
                    "units": format(quantity, "f"),
                    "value_gbp": None,
                    "weight": format(weights.get(ticker, ZERO), "f"),
                }
            )
            continue
        _, session, price, available = quote
        age = _weekdays(session, as_of.date())
        holdings.append(
            {
                "as_of": available.isoformat(),
                "instrument_id": ticker,
                "price_gbp": format(price, "f"),
                "session": session.isoformat(),
                "staleness": "fresh" if age == 0 else "stale",
                "tradable": ticker in tradable and age <= STALE_SESSIONS,
                "units": format(quantity, "f"),
                "value_gbp": format(quantize_money(quantity * price), "f"),
                "weight": format(weights.get(ticker, ZERO), "f"),
            }
        )
    document = {
        "as_of": as_of.isoformat(),
        "constraints": {
            "cash_name": CASH,
            "confidence": "decimal string from 0 to 1",
            "do_not_send": ["action", "planned_risk", "size", "any hash field"],
            "no_trade_band": format(NO_TRADE_BAND, "f"),
            "sum_tolerance": format(SUM_TOLERANCE, "f"),
            "weight": "one decimal string per fund and cash, each from 0 to 1, summing to 1 within the tolerance",
        },
        "instruction": (
            "Return only the allocation JSON. Use prices whose as_of is at or before the decision time. "
            "A missing or stale fund has no fresh price. Do not invent hashes."
        ),
        "market": holdings,
        "output_schema": ALLOCATION_SCHEMA,
        "portfolio": {
            "cash_gbp": format(book.cash, "f"),
            "drawdown": format(book.max_drawdown, "f"),
            "high_water_gbp": format(book.peak, "f"),
            "nav_gbp": format(nav, "f"),
            "pnl_gbp": format(quantize_money(nav - STARTING_CASH_GBP), "f"),
        },
        "task": "paper-v2 allocation",
        "universe": list(FUNDS),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def latest_prices(prints: Sequence[Print], as_of: datetime) -> dict[str, Decimal]:
    chosen: dict[str, tuple[date, Decimal]] = {}
    for ticker, session, price, available in prints:
        if ticker not in FUNDS or available > as_of or price <= 0:
            continue
        current = chosen.get(ticker)
        if current is None or session >= current[0]:
            chosen[ticker] = (session, price)
    return {ticker: price for ticker, (_session, price) in chosen.items()}


def _nav(book: Book, prices: dict[str, Decimal]) -> Decimal:
    total = book.cash
    for ticker in FUNDS:
        price = prices.get(ticker)
        if price is not None:
            total += book.quantity(ticker) * price
    return quantize_money(total)


def _weekdays(start: date, end: date) -> int:
    if end <= start:
        return 0
    count = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            count += 1
    return count

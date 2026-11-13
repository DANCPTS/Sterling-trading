"""Point-in-time prompt for a paper-v2 allocation. The model is not asked for hashes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sterling.research.invest.book import Book
from sterling.research.invest.registration import NO_TRADE_BAND, SUM_TOLERANCE
from sterling.research.invest.schema import ALLOCATION_SCHEMA, CASH, FUNDS
from sterling.research.invest.snapshot import Print, build_invest_snapshot

PROMPT_VERSION = "paper-v2-allocation-2026-11-13"


def allocation_prompt(
    *,
    book: Book,
    prints: Sequence[Print],
    as_of: datetime,
    tradable: set[str],
    sonia: Sequence[tuple[date, Decimal]] = (),
) -> str:
    """JSON the weekly job sends. Prints available after ``as_of`` are omitted."""
    snapshot = build_invest_snapshot(
        book=book,
        prints=prints,
        as_of=as_of,
        requested_tradable=tradable,
        sonia=sonia,
    )
    document = {
        "as_of": snapshot["as_of"],
        "constraints": {
            "cash_name": CASH,
            "change_justification": "why the book should change, or why it should not",
            "confidence": "decimal string from 0 to 1",
            "decision": "ALLOCATE, HOLD, or NO_TRADE",
            "do_not_send": ["planned_risk", "size", "any hash field"],
            "identified_risks": "one or more short strings",
            "model_id": "the configured model name, not a different model",
            "no_trade_band": format(NO_TRADE_BAND, "f"),
            "portfolio_reasoning": "one portfolio-level explanation",
            "sum_tolerance": format(SUM_TOLERANCE, "f"),
            "weight": "one decimal string per fund and cash, each from 0 to 1, summing to 1 within the tolerance",
        },
        "instruction": (
            "Return only the allocation JSON. HOLD and NO_TRADE keep the current book. "
            "Use prices and indicators whose published_at is at or before the decision time. "
            "A missing or stale fund is not tradable. Do not invent a price, a rate, or a hash. "
            "model_id must be the configured model."
        ),
        "output_schema": ALLOCATION_SCHEMA,
        "prompt_version": PROMPT_VERSION,
        "research_snapshot": snapshot,
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

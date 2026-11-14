"""Point-in-time prompt for a paper-v2 allocation. The model is not asked for hashes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sterling.research.invest.book import Book
from sterling.research.invest.registration import NO_TRADE_BAND, SUM_TOLERANCE
from sterling.research.invest.schema import ALLOCATION_SCHEMA, CASH, FUNDS
from sterling.research.invest.snapshot import PricePrint, Print, build_invest_snapshot

PROMPT_VERSION = "paper-v2-allocation-2026-11-14"
PROJECTION_VERSION = "paper-v2-prompt-projection-2026-11-14"
INDICATOR_DEFINITIONS = {
    "drawdown": "latest close versus the peak inside the available history",
    "max_drawdown_252d": "worst close-to-peak decline over 252 sessions",
    "realised_vol_63d": "annualised sample deviation of 63 sessions",
    "return_12m": "252 sessions",
    "return_1m": "21 sessions",
    "return_1w": "5 sessions",
    "return_3m": "63 sessions",
    "return_6m": "126 sessions",
    "trend_vs_10m": "latest close versus the mean of 10 month-end closes",
}


def allocation_prompt(
    *,
    book: Book,
    prints: Sequence[Print],
    as_of: datetime,
    tradable: set[str],
    sonia: Sequence[tuple[date, Decimal]] = (),
    snapshot: dict[str, object] | None = None,
    provenance: str = "supplied",
) -> str:
    """JSON the weekly job sends. Prints available after ``as_of`` are omitted.

    Pass ``snapshot`` to embed the same object the risk gate will read.
    """
    if snapshot is None:
        snapshot = build_invest_snapshot(
            book=book,
            prints=prints,
            as_of=as_of,
            requested_tradable=tradable,
            sonia=sonia,
            provenance=provenance,
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
        "model_view": project_snapshot(snapshot),
        "output_schema": ALLOCATION_SCHEMA,
        "prompt_version": PROMPT_VERSION,
        "task": "paper-v2 allocation",
        "universe": list(FUNDS),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def canonical_snapshot_text(snapshot: dict[str, object]) -> str:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def canonical_snapshot_sha256(snapshot: dict[str, object]) -> str:
    return hashlib.sha256(canonical_snapshot_text(snapshot).encode("utf-8")).hexdigest()


def project_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    """Model-facing view. It does not repeat raw history or indicator source times."""
    portfolio = snapshot.get("portfolio") if isinstance(snapshot.get("portfolio"), dict) else {}
    holdings = portfolio.get("holdings") if isinstance(portfolio, dict) else []
    compact_holdings = []
    if isinstance(holdings, list):
        for row in holdings:
            if not isinstance(row, dict):
                continue
            compact_holdings.append(
                {
                    "availability_basis": row.get("availability_basis"),
                    "available_at": row.get("available_at") or row.get("published_at"),
                    "exchange_close_at": row.get("exchange_close_at"),
                    "instrument_id": row.get("instrument_id"),
                    "price_gbp": row.get("price_gbp"),
                    "provider_published_at": row.get("provider_published_at"),
                    "publication": row.get("publication"),
                    "retrieved_at": row.get("retrieved_at"),
                    "session": row.get("session"),
                    "source": row.get("source"),
                    "staleness": row.get("staleness"),
                    "tradable": row.get("tradable"),
                    "units": row.get("units"),
                    "value_gbp": row.get("value_gbp"),
                    "weight": row.get("weight"),
                }
            )
    indicators = snapshot.get("indicators")
    values = {}
    if isinstance(indicators, list):
        for row in indicators:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                values[row["name"]] = row.get("value")
    counts = snapshot.get("session_counts") if isinstance(snapshot.get("session_counts"), dict) else {}
    return {
        "canonical_snapshot_sha256": canonical_snapshot_sha256(snapshot),
        "as_of": snapshot.get("as_of"),
        "data_availability": snapshot.get("data_availability"),
        "holdings": compact_holdings,
        "indicator_definitions": INDICATOR_DEFINITIONS,
        "indicator_values": values,
        "portfolio": {
            "cash_gbp": portfolio.get("cash_gbp") if isinstance(portfolio, dict) else None,
            "cumulative_return": portfolio.get("cumulative_return") if isinstance(portfolio, dict) else None,
            "high_water_gbp": portfolio.get("high_water_gbp") if isinstance(portfolio, dict) else None,
            "max_drawdown": portfolio.get("max_drawdown") if isinstance(portfolio, dict) else None,
            "nav_gbp": portfolio.get("nav_gbp") if isinstance(portfolio, dict) else None,
            "pnl_gbp": portfolio.get("pnl_gbp") if isinstance(portfolio, dict) else None,
            "receivable_gbp": portfolio.get("receivable_gbp") if isinstance(portfolio, dict) else None,
            "spendable_cash_gbp": portfolio.get("spendable_cash_gbp") if isinstance(portfolio, dict) else None,
            "trades": portfolio.get("trades") if isinstance(portfolio, dict) else None,
            "transaction_costs_gbp": portfolio.get("transaction_costs_gbp") if isinstance(portfolio, dict) else None,
        },
        "projection_version": PROJECTION_VERSION,
        "risk_constraints": snapshot.get("risk_constraints"),
        "session_counts": counts,
        "sonia": snapshot.get("sonia"),
        "source_warnings": _warnings(snapshot, compact_holdings),
        "transaction_cost_model": snapshot.get("transaction_cost_model"),
    }


def _warnings(snapshot: dict[str, object], holdings: list[dict[str, object]]) -> list[str]:
    notes: list[str] = []
    availability = snapshot.get("data_availability")
    basis = ""
    if isinstance(availability, dict):
        basis = str(availability.get("availability_basis") or availability.get("publication") or "")
    if basis == "assumed-next-session":
        notes.append(
            "Close availability is assumed at 08:00 Europe/London on the next XLON session. "
            "That clock is not a verified provider publication time."
        )
    if basis == "synthetic":
        notes.append("Prices are a synthetic fixture. Their timestamps are not market publication times.")
    if any(row.get("availability_basis") == "assumed-next-session" for row in holdings):
        if not any("not a verified provider" in note for note in notes):
            notes.append("At least one close uses assumed next-session availability.")
    notes.append("No second cross-check source is attached to this cache or fixture.")
    stale = [str(row.get("instrument_id")) for row in holdings if row.get("staleness") == "stale"]
    missing = [str(row.get("instrument_id")) for row in holdings if row.get("staleness") == "missing"]
    if stale:
        notes.append("Stale and not tradable: " + ", ".join(stale))
    if missing:
        notes.append("Missing price: " + ", ".join(missing))
    sonia = snapshot.get("sonia")
    if isinstance(sonia, dict) and sonia.get("status") != "published":
        notes.append("SONIA is unavailable: " + str(sonia.get("reason") or sonia.get("status")))
    return notes


def latest_prices(prints: Sequence[Print | PricePrint], as_of: datetime) -> dict[str, Decimal]:
    chosen: dict[str, tuple[date, Decimal]] = {}
    for item in prints:
        if isinstance(item, PricePrint):
            ticker, session, price, available = item.ticker, item.session, item.price, item.available_at
        else:
            ticker, session, price, available = item
        if ticker not in FUNDS or available > as_of or price <= 0:
            continue
        current = chosen.get(ticker)
        if current is None or session >= current[0]:
            chosen[ticker] = (session, price)
    return {ticker: price for ticker, (_session, price) in chosen.items()}

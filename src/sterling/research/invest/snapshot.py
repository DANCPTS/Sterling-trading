"""Point-in-time research snapshot for a paper-v2 decision.

Every price, indicator source, and SONIA print in the snapshot was published
at or before ``as_of``. A missing print stays missing. A stale print is not
described as the current tradable price. Nothing here fetches a feed or
invents an observation.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sterling.research.costs import research_cost_model
from sterling.research.errors import ResearchError
from sterling.research.grok.context import PriceObservation, SoniaObservation, build_market_context
from sterling.research.grok.schema import CashRate
from sterling.research.invest.book import Book, valuation_of
from sterling.research.invest.cash_rate import CashRateQuote, cash_quote, sonia_available_at
from sterling.research.invest.registration import STARTING_CASH_GBP
from sterling.research.invest.schema import FUNDS
from sterling.research.money import ZERO, quantize_money
from sterling.research.risk_config import load_research_limits

Print = tuple[str, date, Decimal, datetime]
STALE_SESSIONS = 5


def build_invest_snapshot(
    *,
    book: Book,
    prints: Sequence[Print],
    as_of: datetime,
    requested_tradable: set[str],
    sonia: Sequence[tuple[date, Decimal]] = (),
    provenance: str = "supplied",
) -> dict[str, object]:
    """Structured snapshot. Values are strings and lists so the decision log can store it.

    ``provenance`` is ``supplied`` when every ``published_at`` came with the
    observation, and ``synthetic-fixture`` when the clock was assigned by a
    fixture. A fixture timestamp is never labelled as a market source.
    """
    stamped = _utc(as_of)
    visible = [
        (ticker, session, price, _utc(published))
        for ticker, session, price, published in prints
        if ticker in FUNDS and price > 0 and _utc(published) <= stamped
    ]
    observations = tuple(
        PriceObservation(
            instrument_id=ticker,
            session=session,
            price=price,
            published_at=published,
            source="paper-v2",
        )
        for ticker, session, price, published in visible
    )
    sonia_rows = _sonia_observations(sonia, stamped)
    context = build_market_context(
        as_of=stamped,
        observations=observations,
        sonia=tuple(sonia_rows),
    )
    latest = _latest(visible)
    prices = {ticker: item[2] for ticker, item in latest.items()}
    valued = valuation_of(book, prices)
    publication = "synthetic" if provenance == "synthetic-fixture" else "source-timestamp"
    source_name = "synthetic-fixture" if provenance == "synthetic-fixture" else "supplied"
    quotes: list[dict[str, object]] = []
    for ticker in FUNDS:
        quantity = book.quantity(ticker)
        quote = latest.get(ticker)
        if quote is None:
            quotes.append(
                {
                    "instrument_id": ticker,
                    "price_gbp": None,
                    "published_at": None,
                    "session": None,
                    "publication": publication,
                    "source": None,
                    "staleness": "missing",
                    "tradable": "false",
                    "units": format(quantity, "f"),
                    "value_gbp": None,
                    "weight": _weight_text(valued, ticker),
                }
            )
            continue
        _ticker, session, price, published = quote
        age = _weekdays(session, as_of.date())
        fresh = age <= STALE_SESSIONS
        tradable = fresh and ticker in requested_tradable
        quotes.append(
            {
                "instrument_id": ticker,
                "price_gbp": format(price, "f"),
                "published_at": published.isoformat(),
                "publication": publication,
                "session": session.isoformat(),
                "source": source_name,
                "staleness": "fresh" if fresh else "stale",
                "tradable": "true" if tradable else "false",
                "units": format(quantity, "f"),
                "value_gbp": format(quantize_money(quantity * price), "f"),
                "weight": _weight_text(valued, ticker),
            }
        )
    limits = load_research_limits()
    model = research_cost_model(as_of.date())
    rate = cash_quote(sonia, as_of.date(), as_of=stamped)
    document: dict[str, object] = {
        "as_of": stamped.isoformat(),
        "data_availability": {
            "as_of": stamped.isoformat(),
            "invented_observations": "none",
            "publication": publication,
            "rule": "published_at <= as_of",
            "valuation": "complete" if valued is not None else "incomplete",
        },
        "indicators": [
            {
                "instrument_id": feature.instrument_id,
                "name": feature.name,
                "published_at": [moment.isoformat() for moment in feature.sources],
                "value": None if feature.value is None else format(feature.value, "f"),
            }
            for feature in context.features
        ],
        "portfolio": _portfolio(book, quotes, valued),
        "price_history": [
            {
                "instrument_id": ticker,
                "price_gbp": format(price, "f"),
                "published_at": published.isoformat(),
                "session": session.isoformat(),
            }
            for ticker, session, price, published in sorted(visible, key=lambda item: (item[0], item[1], item[3]))
        ],
        "risk_constraints": {
            "cash_weight_max": format(limits.cash_max, "f"),
            "cash_weight_min": format(limits.cash_min, "f"),
            "execution_permission": "NOT_ISSUED",
            "max_asset_class_weight": {name: format(cap, "f") for name, cap in limits.max_asset_class_weight},
            "max_gross": format(limits.max_gross, "f"),
            "max_one_way_turnover": format(limits.max_one_way_turnover, "f"),
            "max_single_instrument_weight": format(limits.max_single_instrument_weight, "f"),
            "min_trade_notional_gbp": format(limits.min_trade_notional_gbp, "f"),
            "operational": "false",
            "owner_approved": "false",
            "policy": "research-risk-v0",
            "status": limits.status,
        },
        "sonia": _sonia_block(rate, context.cash_rate),
        "transaction_cost_model": {
            "commission_bps": format(model.commission_bps, "f"),
            "commission_flat_gbp": format(model.commission_flat_gbp, "f"),
            "cumulative_costs_gbp": format(book.cumulative_costs, "f"),
            "slippage_bps": format(model.slippage_bps, "f"),
            "spread_bps": format(model.spread_bps, "f"),
            "stamp_duty_rate": format(model.stamp_duty_rate, "f"),
        },
    }
    if limits.owner_approved:
        raise ResearchError("research snapshot refuses an owner-approved trading policy")
    return document


def _weight_text(valued: dict[str, Decimal] | None, ticker: str) -> str | None:
    if valued is None:
        return None
    return format(valued.get(ticker, ZERO), "f")


def _portfolio(book: Book, quotes: list[dict[str, object]], valued: dict[str, Decimal] | None) -> dict[str, object]:
    if valued is None:
        nav_text = None
        receivable = None
        pnl = None
        cumulative = None
    else:
        nav = valued["nav"]
        nav_text = format(nav, "f")
        receivable = format(valued["receivable"], "f")
        pnl = format(quantize_money(nav - STARTING_CASH_GBP), "f")
        cumulative = format((nav / STARTING_CASH_GBP - 1) if STARTING_CASH_GBP > 0 else ZERO, "f")
    return {
        "cash_gbp": format(book.cash, "f"),
        "cumulative_return": cumulative,
        "high_water_gbp": format(book.peak, "f"),
        "holdings": quotes,
        "max_drawdown": format(book.max_drawdown, "f"),
        "nav_gbp": nav_text,
        "pnl_gbp": pnl,
        "receivable_gbp": receivable if receivable is not None else format(ZERO, "f"),
        "spendable_cash_gbp": format(book.cash, "f"),
        "trades": book.trades,
        "transaction_costs_gbp": format(book.cumulative_costs, "f"),
    }


def tradable_names(snapshot: dict[str, object]) -> set[str]:
    names: set[str] = set()
    for row in _quotes(snapshot):
        name = row.get("instrument_id")
        if row.get("tradable") == "true" and isinstance(name, str):
            names.add(name)
    return names


def stale_names(snapshot: dict[str, object]) -> set[str]:
    return {str(row["instrument_id"]) for row in _quotes(snapshot) if row.get("staleness") == "stale"}


def missing_names(snapshot: dict[str, object]) -> set[str]:
    return {str(row["instrument_id"]) for row in _quotes(snapshot) if row.get("staleness") == "missing"}


def priced_names(snapshot: dict[str, object]) -> set[str]:
    return {str(row["instrument_id"]) for row in _quotes(snapshot) if isinstance(row.get("price_gbp"), str)}


def prices_from_snapshot(snapshot: dict[str, object]) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for row in _quotes(snapshot):
        raw = row.get("price_gbp")
        name = row.get("instrument_id")
        if isinstance(raw, str) and isinstance(name, str):
            prices[name] = Decimal(raw)
    return prices


def _quotes(snapshot: dict[str, object]) -> list[dict[str, object]]:
    portfolio = snapshot.get("portfolio")
    if not isinstance(portfolio, dict):
        return []
    holdings = portfolio.get("holdings")
    if not isinstance(holdings, list):
        return []
    return [row for row in holdings if isinstance(row, dict)]


def _latest(prints: Sequence[tuple[str, date, Decimal, datetime]]) -> dict[str, tuple[str, date, Decimal, datetime]]:
    chosen: dict[str, tuple[str, date, Decimal, datetime]] = {}
    for item in prints:
        current = chosen.get(item[0])
        if current is None or item[1] > current[1] or (item[1] == current[1] and item[3] >= current[3]):
            chosen[item[0]] = item
    return chosen


def _sonia_observations(
    observations: Sequence[tuple[date, Decimal]],
    as_of: datetime,
) -> list[SoniaObservation]:
    rows: list[SoniaObservation] = []
    for reference, percent in observations:
        published = sonia_available_at(reference).astimezone(UTC)
        if published <= as_of:
            rows.append(
                SoniaObservation(
                    observed_on=reference,
                    annual_rate=percent / Decimal(100),
                    published_at=published,
                    source="boe-iadb",
                )
            )
    return rows


def _sonia_block(rate: CashRateQuote | None, wholesale: CashRate | None) -> dict[str, object]:
    published = {
        "publication_lag": "09:00 Europe/London on the next XLON session",
        "series": "IUDSOIA",
        "wholesale_annual_decimal": None if wholesale is None else format(wholesale.annual_rate, "f"),
        "wholesale_observed_on": None if wholesale is None else wholesale.observed_on.isoformat(),
        "wholesale_published_at": None if wholesale is None else wholesale.published_at.isoformat(),
    }
    if rate is None:
        return {
            **published,
            "annual_decimal": None,
            "published_at": None,
            "reference_date": None,
            "sonia_percent": None,
            "status": "unavailable",
        }
    return {
        **published,
        "annual_decimal": format(rate.annual_decimal, "f"),
        "haircut_percentage_points": format(rate.haircut_percentage_points, "f"),
        "published_at": rate.available_at.astimezone(UTC).isoformat(),
        "reference_date": rate.published_on.isoformat(),
        "sonia_percent": format(rate.sonia_percent, "f"),
        "status": "published",
    }


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


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ResearchError("research snapshot timestamps need a timezone")
    return moment.astimezone(UTC)

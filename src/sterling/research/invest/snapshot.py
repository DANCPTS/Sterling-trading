"""Point-in-time research snapshot for a paper-v2 decision.

Every price, indicator source, and SONIA print in the snapshot was published
at or before ``as_of``. A missing print stays missing. A stale print is not
described as the current tradable price. Nothing here fetches a feed or
invents an observation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sterling.research.costs import research_cost_model
from sterling.research.errors import ResearchError
from sterling.research.grok.context import PriceObservation, SoniaObservation, build_market_context
from sterling.research.grok.schema import CashRate
from sterling.research.invest.availability import (
    BASIS_ASSUMED,
    BASIS_SYNTHETIC,
    BASIS_VERIFIED,
    exchange_close_at,
    require_aware,
)
from sterling.research.invest.book import Book, valuation_of
from sterling.research.invest.cash_rate import CashRateQuote, cash_quote, sonia_available_at
from sterling.research.invest.registration import STARTING_CASH_GBP
from sterling.research.invest.schema import FUNDS
from sterling.research.money import ZERO, quantize_money
from sterling.research.risk_config import load_research_limits

Print = tuple[str, date, Decimal, datetime]
STALE_SESSIONS = 5


@dataclass(frozen=True, slots=True)
class PricePrint:
    """One close and the clock that makes it usable."""

    ticker: str
    session: date
    price: Decimal
    available_at: datetime
    exchange_close_at: datetime
    provider_published_at: datetime | None
    retrieved_at: datetime | None
    availability_basis: str
    source: str


def as_price_print(item: PricePrint | Print, *, provenance: str) -> PricePrint:
    """A 4-tuple timestamp is an explicit verified publication time, not a guess."""
    if isinstance(item, PricePrint):
        basis = BASIS_SYNTHETIC if provenance == "synthetic-fixture" else item.availability_basis
        return PricePrint(
            ticker=item.ticker,
            session=item.session,
            price=item.price,
            available_at=require_aware(item.available_at, "available_at"),
            exchange_close_at=require_aware(item.exchange_close_at, "exchange_close_at"),
            provider_published_at=None
            if item.provider_published_at is None
            else require_aware(item.provider_published_at, "provider_published_at"),
            retrieved_at=None if item.retrieved_at is None else require_aware(item.retrieved_at, "retrieved_at"),
            availability_basis=basis,
            source="synthetic-fixture" if provenance == "synthetic-fixture" else item.source,
        )
    ticker, session, price, published = item
    stamped = require_aware(published, "published_at")
    basis = BASIS_SYNTHETIC if provenance == "synthetic-fixture" else BASIS_VERIFIED
    source = "synthetic-fixture" if provenance == "synthetic-fixture" else "supplied"
    return PricePrint(
        ticker=ticker,
        session=session,
        price=price,
        available_at=stamped,
        exchange_close_at=exchange_close_at(session),
        provider_published_at=None if basis == BASIS_SYNTHETIC else stamped,
        retrieved_at=None,
        availability_basis=basis,
        source=source,
    )


def build_invest_snapshot(
    *,
    book: Book,
    prints: Sequence[Print | PricePrint],
    as_of: datetime,
    requested_tradable: set[str],
    sonia: Sequence[tuple[date, Decimal]] = (),
    provenance: str = "supplied",
    sonia_absence_reason: str | None = None,
) -> dict[str, object]:
    """Structured snapshot. Values are strings and lists so the decision log can store it.

    ``provenance`` is ``supplied`` when every ``published_at`` came with the
    observation, and ``synthetic-fixture`` when the clock was assigned by a
    fixture. A fixture timestamp is never labelled as a market source.
    """
    stamped = _utc(as_of)
    normalised = [as_price_print(item, provenance=provenance) for item in prints]
    visible = [item for item in normalised if item.ticker in FUNDS and item.price > 0 and item.available_at <= stamped]
    observations = tuple(
        PriceObservation(
            instrument_id=item.ticker,
            session=item.session,
            price=item.price,
            published_at=item.available_at,
            source="paper-v2",
        )
        for item in visible
    )
    sonia_rows = _sonia_observations(sonia, stamped)
    context = build_market_context(
        as_of=stamped,
        observations=observations,
        sonia=tuple(sonia_rows),
    )
    latest = _latest(visible)
    prices = {item.ticker: item.price for item in latest.values()}
    valued = valuation_of(book, prices)
    publication = _publication_summary(visible, provenance)
    quotes: list[dict[str, object]] = []
    for ticker in FUNDS:
        quantity = book.quantity(ticker)
        quote = latest.get(ticker)
        if quote is None:
            quotes.append(
                {
                    "instrument_id": ticker,
                    "available_at": None,
                    "availability_basis": None,
                    "exchange_close_at": None,
                    "price_gbp": None,
                    "provider_published_at": None,
                    "publication": publication,
                    "published_at": None,
                    "retrieved_at": None,
                    "session": None,
                    "source": None,
                    "staleness": "missing",
                    "tradable": "false",
                    "units": format(quantity, "f"),
                    "value_gbp": None,
                    "weight": _weight_text(valued, ticker),
                }
            )
            continue
        age = _weekdays(quote.session, as_of.date())
        fresh = age <= STALE_SESSIONS
        tradable = fresh and ticker in requested_tradable
        quotes.append(_quote_row(quote, quantity, valued, ticker, "fresh" if fresh else "stale", tradable))
    limits = load_research_limits()
    model = research_cost_model(as_of.date())
    rate = cash_quote(sonia, as_of.date(), as_of=stamped)
    document: dict[str, object] = {
        "as_of": stamped.isoformat(),
        "data_availability": {
            "as_of": stamped.isoformat(),
            "invented_observations": "none",
            "availability_basis": publication,
            "publication": publication,
            "rule": "available_at <= as_of",
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
            _history_row(item) for item in sorted(visible, key=lambda item: (item.ticker, item.session, item.available_at))
        ],
        "session_counts": {ticker: sum(1 for item in visible if item.ticker == ticker) for ticker in FUNDS},
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
        "sonia": _sonia_block(rate, context.cash_rate, sonia_absence_reason if rate is None else None),
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


def _latest(prints: Sequence[PricePrint]) -> dict[str, PricePrint]:
    chosen: dict[str, PricePrint] = {}
    for item in prints:
        current = chosen.get(item.ticker)
        if (
            current is None
            or item.session > current.session
            or (item.session == current.session and item.available_at >= current.available_at)
        ):
            chosen[item.ticker] = item
    return chosen


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.isoformat()


def _quote_row(
    quote: PricePrint,
    quantity: Decimal,
    valued: dict[str, Decimal] | None,
    ticker: str,
    staleness: str,
    tradable: bool,
) -> dict[str, object]:
    return {
        "available_at": quote.available_at.isoformat(),
        "availability_basis": quote.availability_basis,
        "exchange_close_at": quote.exchange_close_at.isoformat(),
        "instrument_id": ticker,
        "price_gbp": format(quote.price, "f"),
        "provider_published_at": _iso(quote.provider_published_at),
        "publication": quote.availability_basis,
        "published_at": quote.available_at.isoformat(),
        "retrieved_at": _iso(quote.retrieved_at),
        "session": quote.session.isoformat(),
        "source": quote.source,
        "staleness": staleness,
        "tradable": "true" if tradable else "false",
        "units": format(quantity, "f"),
        "value_gbp": format(quantize_money(quantity * quote.price), "f"),
        "weight": _weight_text(valued, ticker),
    }


def _history_row(item: PricePrint) -> dict[str, object]:
    return {
        "available_at": item.available_at.isoformat(),
        "availability_basis": item.availability_basis,
        "exchange_close_at": item.exchange_close_at.isoformat(),
        "instrument_id": item.ticker,
        "price_gbp": format(item.price, "f"),
        "provider_published_at": _iso(item.provider_published_at),
        "published_at": item.available_at.isoformat(),
        "retrieved_at": _iso(item.retrieved_at),
        "session": item.session.isoformat(),
        "source": item.source,
    }


def _publication_summary(visible: Sequence[PricePrint], provenance: str) -> str:
    if provenance == "synthetic-fixture":
        return BASIS_SYNTHETIC
    bases = {item.availability_basis for item in visible}
    if BASIS_ASSUMED in bases:
        return BASIS_ASSUMED
    return BASIS_VERIFIED


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


def _sonia_block(rate: CashRateQuote | None, wholesale: CashRate | None, absence_reason: str | None) -> dict[str, object]:
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
            "reason": absence_reason or "sonia_not_yet_published",
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

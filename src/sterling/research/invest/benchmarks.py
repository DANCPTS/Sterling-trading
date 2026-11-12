"""Same-start benchmarks and the shared performance JSON."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal

from sterling.research.invest.book import Book, accrue_cash, apply_targets, mark
from sterling.research.invest.cash_rate import cash_quote
from sterling.research.invest.identity import INVEST_V2, performance_document
from sterling.research.invest.registration import STARTING_CASH_GBP
from sterling.research.invest.schema import CASH, FUNDS
from sterling.research.metrics import sharpe_ratio
from sterling.research.money import ZERO
from sterling.research.strategies.dual_momentum import DualMomentum
from sterling.research.strategies.trend import TrendFilter
from sterling.research.strategy import MarketView

_FROZEN_TREND = TrendFilter(lookback_months=10, defensive=None)
_FROZEN_DUAL = DualMomentum(lookback_months=12, top_k=2, defensive=None)


def summarise_arm(
    name: str,
    rows: Sequence[tuple[date, Decimal]],
    *,
    trades: int,
    costs: Decimal,
) -> dict[str, object]:
    end = rows[-1][1] if rows else STARTING_CASH_GBP
    daily: list[Decimal] = []
    peak = STARTING_CASH_GBP
    drawdown = ZERO
    prior = STARTING_CASH_GBP
    for _day, nav in rows:
        if prior > 0:
            daily.append(nav / prior - 1)
        if nav > peak:
            peak = nav
        if peak > 0 and (peak - nav) / peak > drawdown:
            drawdown = (peak - nav) / peak
        prior = nav
    deviation = _stdev(daily)
    volatility = None if deviation is None else deviation * Decimal(252).sqrt()
    span = (rows[-1][0] - rows[0][0]).days if len(rows) >= 2 else 0
    annualised = None
    if span > 0 and STARTING_CASH_GBP > 0 and end > 0:
        annualised = (end / STARTING_CASH_GBP) ** (Decimal("365.25") / Decimal(span)) - 1
    total = (end / STARTING_CASH_GBP - 1) if rows and STARTING_CASH_GBP > 0 else ZERO
    return {
        "annualised_return": _text(annualised),
        "costs_gbp": format(costs, "f"),
        "cumulative_return": format(total, "f"),
        "end_nav_gbp": format(end, "f"),
        "max_drawdown": format(drawdown, "f"),
        "name": name,
        "sharpe": _text(sharpe_ratio(daily)),
        "start_nav_gbp": format(STARTING_CASH_GBP, "f"),
        "trades": trades,
        "volatility": _text(volatility),
    }


def benchmark_table(
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    *,
    sonia: Sequence[tuple[date, Decimal]],
    as_of: datetime | None = None,
) -> dict[str, object]:
    """Replay frozen rules on the same sessions, costs and SONIA rate.

    Interest is one calendar day at a time, including weekends between the
    sessions, at the latest IUDSOIA print already published at ``as_of``.
    That is the same rule as the paper book. A missing print accrues nothing
    for that day and is not stored as zero.
    """
    arms = [
        _static("buy_and_hold_vwrp", {"VWRP": Decimal(1)}, closes, sessions, sonia, as_of),
        _static("equal_weight", {name: Decimal(1) / Decimal(len(FUNDS)) for name in FUNDS}, closes, sessions, sonia, as_of),
        _static("sixty_forty", {"VWRP": Decimal("0.60"), "IGLT": Decimal("0.40")}, closes, sessions, sonia, as_of),
        _cash_only(sessions, sonia, as_of),
        _frozen("etf_trend", _FROZEN_TREND, closes, sessions, sonia, as_of),
        _frozen("etf_dual_momentum", _FROZEN_DUAL, closes, sessions, sonia, as_of),
    ]
    return performance_document(experiment=INVEST_V2, arms=arms)


def _accrue_span(
    book: Book,
    sonia: Sequence[tuple[date, Decimal]],
    previous: date | None,
    session: date,
    as_of: datetime | None,
) -> None:
    start = session if previous is None else previous + timedelta(days=1)
    day = start
    while day <= session:
        quote = cash_quote(sonia, day, as_of=as_of)
        if quote is not None and quote.annual_decimal > 0:
            accrue_cash(book, quote.annual_decimal)
        day += timedelta(days=1)


def _static(
    name: str,
    weights: dict[str, Decimal],
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    sonia: Sequence[tuple[date, Decimal]],
    as_of: datetime | None,
) -> dict[str, object]:
    targets = {name: ZERO for name in FUNDS}
    targets.update(weights)
    targets[CASH] = Decimal(1) - sum(weights.values(), ZERO)
    return _once(name, targets, closes, sessions, sonia, as_of)


def _once(
    name: str,
    targets: dict[str, Decimal],
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    sonia: Sequence[tuple[date, Decimal]],
    as_of: datetime | None,
) -> dict[str, object]:
    book = Book()
    rows: list[tuple[date, Decimal]] = []
    invested = False
    previous: date | None = None
    for session in sessions:
        _accrue_span(book, sonia, previous, session, as_of)
        previous = session
        prices = _on(closes, session)
        if not invested:
            tradable = {ticker for ticker in FUNDS if ticker in prices}
            apply_targets(book, targets, prices, tradable, session)
            invested = True
        marked = mark(book, prices, session)
        if marked is not None:
            rows.append((session, marked[0]))
    return summarise_arm(name, rows, trades=book.trades, costs=book.cumulative_costs)


def _cash_only(
    sessions: Sequence[date],
    sonia: Sequence[tuple[date, Decimal]],
    as_of: datetime | None,
) -> dict[str, object]:
    book = Book()
    rows: list[tuple[date, Decimal]] = []
    previous: date | None = None
    for session in sessions:
        _accrue_span(book, sonia, previous, session, as_of)
        previous = session
        marked = mark(book, {}, session)
        if marked is not None:
            rows.append((session, marked[0]))
    return summarise_arm("sterling_cash", rows, trades=0, costs=ZERO)


def _frozen(
    name: str,
    strategy: TrendFilter | DualMomentum,
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    sonia: Sequence[tuple[date, Decimal]],
    as_of: datetime | None,
) -> dict[str, object]:
    if not sessions:
        return summarise_arm(name, [], trades=0, costs=ZERO)
    view = MarketView(
        as_of=sessions[-1],
        closes={ticker: tuple(points) for ticker, points in closes.items()},
    )
    raw = strategy.target_weights(view, FUNDS)
    targets = {ticker: Decimal(raw.get(ticker, ZERO)) for ticker in FUNDS}
    targets[CASH] = Decimal(1) - sum(targets.values(), ZERO)
    if targets[CASH] < 0:
        targets = {ticker: ZERO for ticker in FUNDS}
        targets[CASH] = Decimal(1)
    return _once(name, targets, closes, sessions, sonia, as_of)


def _on(closes: Mapping[str, Sequence[tuple[date, Decimal]]], session: date) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for ticker, points in closes.items():
        for day, price in points:
            if day == session:
                found[ticker] = price
    return found


def _stdev(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = sum(values, ZERO) / Decimal(len(values))
    accumulator = sum(((value - mean) ** 2 for value in values), ZERO)
    return (accumulator / Decimal(len(values) - 1)).sqrt()


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")

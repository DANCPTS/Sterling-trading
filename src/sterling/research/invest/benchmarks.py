"""Same-start benchmarks and the shared performance JSON."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from sterling.research.invest.book import Book, accrue_cash, apply_targets, mark
from sterling.research.invest.cash_rate import cash_quote
from sterling.research.invest.identity import INVEST_V2, performance_document
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
    start = rows[0][1] if rows else ZERO
    end = rows[-1][1] if rows else start
    daily: list[Decimal] = []
    peak = start
    drawdown = ZERO
    for earlier, later in zip(rows, rows[1:], strict=False):
        if earlier[1] > 0:
            daily.append(later[1] / earlier[1] - 1)
        if later[1] > peak:
            peak = later[1]
        if peak > 0:
            gap = (peak - later[1]) / peak
            if gap > drawdown:
                drawdown = gap
    deviation = _stdev(daily)
    volatility = None if deviation is None else deviation * Decimal(252).sqrt()
    span = (rows[-1][0] - rows[0][0]).days if len(rows) >= 2 else 0
    annualised = None
    if span > 0 and start > 0 and end > 0:
        annualised = (end / start) ** (Decimal("365.25") / Decimal(span)) - 1
    total = (end / start - 1) if start > 0 else ZERO
    return {
        "annualised_return": _text(annualised),
        "costs_gbp": format(costs, "f"),
        "cumulative_return": format(total, "f"),
        "end_nav_gbp": format(end, "f"),
        "max_drawdown": format(drawdown, "f"),
        "name": name,
        "sharpe": _text(sharpe_ratio(daily)),
        "start_nav_gbp": format(start, "f"),
        "trades": trades,
        "volatility": _text(volatility),
    }


def benchmark_table(
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    *,
    sonia: Sequence[tuple[date, Decimal]],
) -> dict[str, object]:
    """Replay frozen rules and the static mixes on the same sessions, costs and SONIA rate."""
    arms = [
        _static("buy_and_hold_vwrp", {"VWRP": Decimal(1)}, closes, sessions, sonia),
        _static("equal_weight", {name: Decimal(1) / Decimal(len(FUNDS)) for name in FUNDS}, closes, sessions, sonia),
        _static("sixty_forty", {"VWRP": Decimal("0.60"), "IGLT": Decimal("0.40")}, closes, sessions, sonia),
        _cash_only(sessions, sonia),
        _frozen("etf_trend", _FROZEN_TREND, closes, sessions, sonia),
        _frozen("etf_dual_momentum", _FROZEN_DUAL, closes, sessions, sonia),
    ]
    return performance_document(experiment=INVEST_V2, arms=arms)


def _accrue(book: Book, sonia: Sequence[tuple[date, Decimal]], session: date) -> None:
    quote = cash_quote(sonia, session)
    if quote is not None and quote.annual_decimal > 0:
        accrue_cash(book, quote.annual_decimal)


def _static(
    name: str,
    weights: dict[str, Decimal],
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    sonia: Sequence[tuple[date, Decimal]],
) -> dict[str, object]:
    targets = {name: ZERO for name in FUNDS}
    targets.update(weights)
    targets[CASH] = Decimal(1) - sum(weights.values(), ZERO)
    return _once(name, targets, closes, sessions, sonia)


def _once(
    name: str,
    targets: dict[str, Decimal],
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    sonia: Sequence[tuple[date, Decimal]],
) -> dict[str, object]:
    book = Book()
    rows: list[tuple[date, Decimal]] = []
    invested = False
    for session in sessions:
        _accrue(book, sonia, session)
        prices = _on(closes, session)
        if not invested:
            tradable = {ticker for ticker in FUNDS if ticker in prices}
            apply_targets(book, targets, prices, tradable, session)
            invested = True
        marked = mark(book, prices, session)
        if marked is not None:
            rows.append((session, marked[0]))
    return summarise_arm(name, rows, trades=book.trades, costs=book.cumulative_costs)


def _cash_only(sessions: Sequence[date], sonia: Sequence[tuple[date, Decimal]]) -> dict[str, object]:
    book = Book()
    rows: list[tuple[date, Decimal]] = []
    for session in sessions:
        _accrue(book, sonia, session)
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
    return _once(name, targets, closes, sessions, sonia)


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

"""Same-start benchmarks and the shared performance JSON."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sterling.research.calendar import LONDON, xlon
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

# Rows calculated before this id used the final session for every earlier
# decision and one terminal SONIA clock. Those stored rows are not rewritten.
POINT_IN_TIME_CALCULATION = "point-in-time-v1"
LEGACY_BENCHMARK_RULE = (
    "A stored performance row without benchmark_calculation point-in-time-v1 "
    "is a legacy result. It may use later prices in an earlier decision and a "
    "terminal SONIA clock. The stored figures are not rewritten and need "
    "recalculation before comparison with a valid benchmark."
)

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
    publications: Mapping[str, Sequence[tuple[date, datetime]]] | None = None,
) -> dict[str, object]:
    """Replay frozen rules on the same sessions, costs and SONIA rate.

    Interest for each calendar day uses only an IUDSOIA print published by
    16:30 Europe/London that day, and never a print published after the report
    ``as_of``. A frozen rule decides at a month-end using closes on or before
    that session and trades at the next session. A missing print accrues
    nothing for that day and is not stored as zero.
    """
    closes = _published(closes, publications, as_of)
    arms = [
        _static("buy_and_hold_vwrp", {"VWRP": Decimal(1)}, closes, sessions, sonia, as_of),
        _static("equal_weight", {name: Decimal(1) / Decimal(len(FUNDS)) for name in FUNDS}, closes, sessions, sonia, as_of),
        _static("sixty_forty", {"VWRP": Decimal("0.60"), "IGLT": Decimal("0.40")}, closes, sessions, sonia, as_of),
        _cash_only(sessions, sonia, as_of),
        _frozen("etf_trend", _FROZEN_TREND, closes, sessions, sonia, as_of),
        _frozen("etf_dual_momentum", _FROZEN_DUAL, closes, sessions, sonia, as_of),
    ]
    for arm in arms:
        arm["calculation"] = POINT_IN_TIME_CALCULATION
        arm["look_ahead"] = "excluded"
        _execution(arm)
    document = performance_document(experiment=INVEST_V2, arms=arms)
    document["benchmark_calculation"] = POINT_IN_TIME_CALCULATION
    document["benchmark_status"] = "valid"
    document["execution_equivalence"] = "not_claimed"
    document["execution_note"] = (
        "Static arms enter at the first session close. Dynamic arms decide at a month-end close "
        "and fill at a later session close. Opens are not in this series, so a close is not used "
        "as a silent substitute for an open. These conventions are provisional and are not fully "
        "execution-equivalent to each other or to the paper-v2 next open."
    )
    document["legacy_benchmark_rule"] = LEGACY_BENCHMARK_RULE
    document["publication_rule"] = (
        "A supplied published_at after that session's 16:30 London clock is excluded. "
        "A close with no publication time is available at that session and not before it."
    )
    return document


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
        quote = cash_quote(sonia, day, as_of=_day_clock(day, as_of))
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


def point_in_time_path(
    strategy: TrendFilter | DualMomentum,
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    sonia: Sequence[tuple[date, Decimal]],
    as_of: datetime | None = None,
    publications: Mapping[str, Sequence[tuple[date, datetime]]] | None = None,
) -> tuple[tuple[date, Decimal, dict[str, str]], ...]:
    """NAV and the decision weights at each session, using only data known then."""
    book, rows = _walk_frozen(strategy, _published(closes, publications, as_of), sessions, sonia, as_of)
    del book
    return tuple(rows)


def frozen_book(
    strategy: TrendFilter | DualMomentum,
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    sonia: Sequence[tuple[date, Decimal]],
    as_of: datetime | None = None,
) -> Book:
    """The book after a point-in-time replay. A session with no close is not filled."""
    book, _rows = _walk_frozen(strategy, closes, sessions, sonia, as_of)
    return book


def _frozen(
    name: str,
    strategy: TrendFilter | DualMomentum,
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    sonia: Sequence[tuple[date, Decimal]],
    as_of: datetime | None,
) -> dict[str, object]:
    book, rows = _walk_frozen(strategy, closes, sessions, sonia, as_of)
    return summarise_arm(name, [(day, nav) for day, nav, _weights in rows], trades=book.trades, costs=book.cumulative_costs)


def _walk_frozen(
    strategy: TrendFilter | DualMomentum,
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    sessions: Sequence[date],
    sonia: Sequence[tuple[date, Decimal]],
    as_of: datetime | None,
) -> tuple[Book, list[tuple[date, Decimal, dict[str, str]]]]:
    book = Book()
    rows: list[tuple[date, Decimal, dict[str, str]]] = []
    if not sessions:
        return book, rows
    pending: dict[str, Decimal] | None = None
    previous: date | None = None
    month_ends = set(xlon().month_ends(sessions[0], sessions[-1]))
    for session in sessions:
        _accrue_span(book, sonia, previous, session, as_of)
        previous = session
        prices = _on(_visible(closes, session), session)
        if pending is not None:
            tradable = {ticker for ticker in FUNDS if ticker in prices}
            apply_targets(book, pending, prices, tradable, session)
            pending = None
        marked = mark(book, prices, session)
        decided: dict[str, str] = {}
        if session in month_ends:
            pending = _targets(strategy, _visible(closes, session), session)
            decided = {name: format(pending[name], "f") for name in sorted(pending)}
        if marked is not None:
            rows.append((session, marked[0], decided))
    return book, rows


def _visible(
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    session: date,
) -> dict[str, tuple[tuple[date, Decimal], ...]]:
    return {ticker: tuple((day, price) for day, price in points if day <= session) for ticker, points in closes.items()}


def _targets(
    strategy: TrendFilter | DualMomentum,
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    session: date,
) -> dict[str, Decimal]:
    view = MarketView(as_of=session, closes={ticker: tuple(points) for ticker, points in closes.items()})
    raw = strategy.target_weights(view, FUNDS)
    targets = {ticker: Decimal(raw.get(ticker, ZERO)) for ticker in FUNDS}
    targets[CASH] = Decimal(1) - sum((targets[ticker] for ticker in FUNDS), ZERO)
    if targets[CASH] < 0:
        targets = {ticker: ZERO for ticker in FUNDS}
        targets[CASH] = Decimal(1)
    return targets


def _execution(arm: dict[str, object]) -> None:
    name = str(arm.get("name"))
    if name in {"etf_trend", "etf_dual_momentum"}:
        convention = "decide_at_month_end_close_fill_next_session_close"
        executable = "later_session_close_when_present"
    elif name == "sterling_cash":
        convention = "cash_interest_only"
        executable = "none"
    else:
        convention = "enter_at_first_session_close"
        executable = "first_session_close"
    arm["cash_interest"] = "SONIA with publication lag, same starting capital"
    arm["costs"] = "research spread, slippage, and commission; ETF stamp exempt"
    arm["decision_price"] = "close_on_or_before_the_decision"
    arm["executable_price"] = executable
    arm["execution_comparability"] = "provisional_not_fully_execution_equivalent"
    arm["execution_convention"] = convention
    arm["open_substitution"] = "none"
    arm["valuation_calendar"] = "shared_sessions"


def _published(
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    publications: Mapping[str, Sequence[tuple[date, datetime]]] | None,
    as_of: datetime | None,
) -> dict[str, tuple[tuple[date, Decimal], ...]]:
    if not publications:
        return {ticker: tuple(points) for ticker, points in closes.items()}
    filtered: dict[str, tuple[tuple[date, Decimal], ...]] = {}
    for ticker, points in closes.items():
        available = {day: moment for day, moment in publications.get(ticker, ())}
        kept: list[tuple[date, Decimal]] = []
        for day, price in points:
            moment = available.get(day)
            if moment is not None and moment > _day_clock(day, as_of):
                continue
            kept.append((day, price))
        filtered[ticker] = tuple(kept)
    return filtered


def _day_clock(day: date, ceiling: datetime | None) -> datetime:
    """16:30 London on ``day``, capped by the report clock so a later print stays hidden."""
    local = datetime.combine(day, time(16, 30), tzinfo=LONDON).astimezone(UTC)
    if ceiling is None:
        return local
    cap = ceiling.astimezone(UTC)
    return local if local <= cap else cap


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

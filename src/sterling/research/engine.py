"""Research-only simulator.

Signals are evaluated at the month-end close using bars on or before that
session. Fills are at the next session's open. The engine never submits an
order. ``execution_permission`` stays ``NOT_ISSUED``.

Exchange prices are already net of fund or issuer charges, so OCF is not
deducted. ``deduct_ocf=True`` is refused.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sterling.research.bars import Bar, PreparedSeries, prepare_series
from sterling.research.calendar import WeekdayCalendar
from sterling.research.costs import CostModel, TradeQuote, affordable_buy, quote_trade
from sterling.research.errors import DataValidationError, ResearchError
from sterling.research.instruments import Instrument
from sterling.research.isolation import EXECUTION_PERMISSION, ORDERS_SUBMITTED, RESEARCH_MODE, assert_not_on_execution_path
from sterling.research.metrics import NavRow, Performance, performance
from sterling.research.money import ZERO, ZERO_MONEY, quantize_money, quantize_qty_down, quantize_weight_down
from sterling.research.proposal import RulesProposalSource
from sterling.research.risk_config import ResearchLimits
from sterling.research.risk_gate import apply_risk_gate
from sterling.research.strategy import MarketView, Strategy

OCF_NOTE = (
    "Exchange prices are already net of fund or issuer charges. "
    "This run deducted GBP 0.00 of OCF, TER, or all-in fee. "
    "Deducting those charges again is refused. gross_index_fee_drag is a separate disclosure helper "
    "for a labelled gross index proxy and is not applied here."
)


@dataclass(frozen=True, slots=True)
class Fill:
    session: date
    signal_date: date | None
    ticker: str
    side: str
    quantity: Decimal
    mid_price_gbp: Decimal
    exec_price_gbp: Decimal
    notional_gbp: Decimal
    commission_gbp: Decimal
    spread_cost_gbp: Decimal
    slippage_cost_gbp: Decimal
    stamp_duty_gbp: Decimal
    fx_cost_gbp: Decimal
    cash_after_gbp: Decimal
    total_cost_gbp: Decimal


@dataclass(frozen=True, slots=True)
class SimulationResult:
    mode: str
    execution_permission: str
    orders_submitted: int
    label: str
    nav: tuple[NavRow, ...]
    fills: tuple[Fill, ...]
    proposals: tuple[dict[str, object], ...]
    decisions: tuple[dict[str, object], ...]
    metrics: Performance
    benchmarks: tuple[tuple[str, Performance], ...]
    caveats: tuple[str, ...]
    ocf_deducted_gbp: Decimal
    total_cost_gbp: Decimal
    total_turnover: Decimal


@dataclass
class _Book:
    cash: Decimal
    quantity: dict[str, Decimal]


def _material_change(
    targets: Mapping[str, Decimal],
    current: Mapping[str, Decimal],
    nav: Decimal,
    min_trade: Decimal,
) -> bool:
    """True when the close-of-signal weight change is large enough to send."""
    names = set(targets) | set(current)
    for name in names:
        if abs(targets.get(name, ZERO) - current.get(name, ZERO)) * nav >= min_trade:
            return True
    return False


def build_view(series: Mapping[str, PreparedSeries], as_of: date) -> MarketView:
    closes: dict[str, tuple[tuple[date, Decimal], ...]] = {}
    for ticker, prepared in series.items():
        closes[ticker] = tuple((bar.session, bar.signal_close) for bar in prepared.bars if bar.session <= as_of)
    return MarketView(as_of=as_of, closes=closes)


def _prices(prepared: Mapping[str, PreparedSeries], day: date, field: str) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for ticker, series in prepared.items():
        match = next((bar for bar in series.bars if bar.session == day), None)
        if match is None:
            raise DataValidationError(f"{ticker} has no bar on {day.isoformat()}")
        prices[ticker] = match.open if field == "open" else match.close
    return prices


def _mark(book: _Book, prices: Mapping[str, Decimal]) -> tuple[Decimal, Decimal, dict[str, Decimal]]:
    market_value = sum(
        (quantity * prices[ticker] for ticker, quantity in book.quantity.items() if quantity > 0),
        ZERO,
    )
    nav = book.cash + market_value
    if nav <= 0:
        raise ResearchError("NAV is not positive")
    weights = {ticker: quantity * prices[ticker] / nav for ticker, quantity in book.quantity.items() if quantity > 0}
    gross = market_value / nav
    return nav, gross, weights


def _fill_from_quote(
    quote: TradeQuote,
    *,
    ticker: str,
    session: date,
    signal_date: date | None,
    cash_after: Decimal,
) -> Fill:
    return Fill(
        session=session,
        signal_date=signal_date,
        ticker=ticker,
        side=quote.side,
        quantity=quote.quantity,
        mid_price_gbp=quote.mid_price_gbp,
        exec_price_gbp=quote.exec_price_gbp,
        notional_gbp=quote.notional_gbp,
        commission_gbp=quote.commission_gbp,
        spread_cost_gbp=quote.spread_cost_gbp,
        slippage_cost_gbp=quote.slippage_cost_gbp,
        stamp_duty_gbp=quote.stamp_duty_gbp,
        fx_cost_gbp=quote.fx_cost_gbp,
        cash_after_gbp=cash_after,
        total_cost_gbp=quote.total_cost_gbp,
    )


def _rebalance(
    book: _Book,
    targets: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    instruments: Mapping[str, Instrument],
    model: CostModel,
    min_trade: Decimal,
    session: date,
    signal_date: date | None,
) -> tuple[list[Fill], Decimal]:
    gross_target = sum(targets.values(), ZERO)
    if gross_target > Decimal(1) or any(weight < 0 for weight in targets.values()):
        raise ResearchError("execution refused leverage or a short")
    nav, _gross, pre_weights = _mark(book, prices)
    target_qty = {ticker: quantize_qty_down(targets.get(ticker, ZERO) * nav / prices[ticker]) for ticker in prices}
    names = sorted(prices)
    turnover_base = dict(pre_weights)
    fills: list[Fill] = []

    def flags(ticker: str) -> tuple[bool, bool]:
        instrument = instruments[ticker]
        return (not instrument.stamp_duty_exempt, instrument.trading_currency != "GBP")

    def trade(ticker: str, side: str, quantity: Decimal) -> None:
        if quantity <= 0:
            return
        stamp, charge_fx = flags(ticker)
        mid = prices[ticker]
        if quantity * mid < min_trade:
            return
        if side == "SELL":
            held = book.quantity.get(ticker, ZERO)
            quantity = min(quantity, held)
            if quantity <= 0 or quantity * mid < min_trade:
                return
            quote = quote_trade(side, quantity, mid, model, stamp=False, charge_fx=charge_fx)
        else:
            desired = quote_trade(side, quantity, mid, model, stamp=stamp, charge_fx=charge_fx)
            if -desired.cash_delta_gbp <= book.cash:
                quote = desired
            else:
                quote = affordable_buy(book.cash, mid, model, stamp=stamp, charge_fx=charge_fx)
                if quote.quantity > quantity:
                    quote = quote_trade(side, quantity, mid, model, stamp=stamp, charge_fx=charge_fx)
        if quote.quantity == 0:
            return
        book.cash = quantize_money(book.cash + quote.cash_delta_gbp)
        if book.cash < 0:
            raise ResearchError("execution refused a fill that would make cash negative")
        held = book.quantity.get(ticker, ZERO)
        if side == "BUY":
            book.quantity[ticker] = held + quote.quantity
        else:
            remaining = held - quote.quantity
            if remaining <= 0:
                book.quantity.pop(ticker, None)
            else:
                book.quantity[ticker] = remaining
        fills.append(_fill_from_quote(quote, ticker=ticker, session=session, signal_date=signal_date, cash_after=book.cash))

    for ticker in names:
        current = book.quantity.get(ticker, ZERO)
        goal = target_qty[ticker]
        if current > goal:
            trade(ticker, "SELL", current - goal)
    for ticker in names:
        current = book.quantity.get(ticker, ZERO)
        goal = target_qty[ticker]
        if goal > current:
            trade(ticker, "BUY", goal - current)
    _nav, _gross_after, post_weights = _mark(book, prices)
    keys = set(turnover_base) | set(post_weights)
    one_way = sum((abs(post_weights.get(key, ZERO) - turnover_base.get(key, ZERO)) for key in keys), ZERO) / Decimal(2)
    return fills, one_way


def _dividends(
    book: _Book,
    prepared: Mapping[str, PreparedSeries],
    day: date,
    instruments: Mapping[str, Instrument],
    model: CostModel,
) -> list[Fill]:
    fills: list[Fill] = []
    for ticker, series in prepared.items():
        if series.dividend_mode == "ignore":
            continue
        bar = next(item for item in series.bars if item.session == day)
        held = book.quantity.get(ticker, ZERO)
        if bar.dividend <= 0 or held <= 0:
            continue
        income = quantize_money(held * bar.dividend)
        if income <= 0:
            continue
        book.cash = quantize_money(book.cash + income)
        if series.dividend_mode == "cash":
            continue
        stamp = not instruments[ticker].stamp_duty_exempt
        charge_fx = instruments[ticker].trading_currency != "GBP"
        quote = affordable_buy(income, bar.close, model, stamp=stamp, charge_fx=charge_fx)
        if quote.quantity == 0:
            continue
        book.cash = quantize_money(book.cash + quote.cash_delta_gbp)
        if book.cash < 0:
            raise ResearchError("distribution reinvestment would make cash negative")
        book.quantity[ticker] = held + quote.quantity
        fills.append(_fill_from_quote(quote, ticker=ticker, session=day, signal_date=None, cash_after=book.cash))
    return fills


def _run_path(
    *,
    prepared: Mapping[str, PreparedSeries],
    instruments: Mapping[str, Instrument],
    calendar: WeekdayCalendar,
    model: CostModel,
    limits: ResearchLimits,
    starting_nav: Decimal,
    history_start: date,
    evaluate_start: date,
    evaluate_end: date,
    strategy: Strategy | None,
    static_weights: Mapping[str, Decimal] | None,
    apply_gate: bool,
    monthly_rebalance: bool = False,
    no_trade_band: Decimal = ZERO,
    cost_on: Callable[[date], CostModel] | None = None,
) -> tuple[tuple[NavRow, ...], tuple[Fill, ...], tuple[dict[str, object], ...], tuple[dict[str, object], ...], Decimal, Decimal]:
    sessions = tuple(prepared[next(iter(prepared))].bars)
    session_days = tuple(bar.session for bar in sessions)
    for series in prepared.values():
        if tuple(bar.session for bar in series.bars) != session_days:
            raise DataValidationError("instrument sessions do not match")
    book = _Book(cash=starting_nav, quantity={})
    rows: list[NavRow] = []
    fills: list[Fill] = []
    proposals: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    total_turnover = ZERO
    pending: tuple[date, dict[str, Decimal], date] | None = None
    entered_static = False
    source = RulesProposalSource(strategy) if strategy is not None else None
    month_ends = set(calendar.month_ends(history_start, evaluate_end))
    asset_class = {ticker: instrument.asset_class for ticker, instrument in instruments.items()}

    for day in session_days:
        if day < history_start or day > evaluate_end:
            continue
        opens = _prices(prepared, day, "open")
        day_model = cost_on(day) if cost_on is not None else model
        if static_weights is not None and not entered_static and day >= evaluate_start:
            static_fills, traded = _rebalance(
                book, static_weights, opens, instruments, day_model, limits.min_trade_notional_gbp, day, None
            )
            fills.extend(static_fills)
            total_turnover += traded
            entered_static = True
        if pending is not None and pending[0] == day and day >= evaluate_start:
            _fill_day, weights, signal_date = pending
            traded_fills, traded = _rebalance(
                book, weights, opens, instruments, day_model, limits.min_trade_notional_gbp, day, signal_date
            )
            fills.extend(traded_fills)
            total_turnover += traded
            pending = None
        dividend_fills = _dividends(book, prepared, day, instruments, day_model)
        fills.extend(dividend_fills)
        closes = _prices(prepared, day, "close")
        nav, gross, weights_now = _mark(book, closes)
        if gross > Decimal("1.00000001"):
            raise ResearchError("gross exposure exceeded 100%")
        if book.cash < 0:
            raise ResearchError("cash went negative")
        if evaluate_start <= day <= evaluate_end:
            rows.append(
                NavRow(
                    session=day,
                    cash_gbp=book.cash,
                    market_value_gbp=quantize_money(nav - book.cash),
                    nav_gbp=quantize_money(nav),
                    gross_exposure=gross,
                    cash_weight=book.cash / nav,
                    holdings=tuple(sorted(weights_now.items())),
                )
            )
        if monthly_rebalance and static_weights is not None and day in month_ends:
            nxt_rebalance = calendar.next_trading_day(day)
            if evaluate_start <= nxt_rebalance <= evaluate_end:
                pending = (nxt_rebalance, dict(static_weights), day)
        if source is not None and day in month_ends:
            view = build_view(prepared, day)
            proposal = source.propose(
                view,
                tuple(instruments),
                decision_timestamp=calendar.session_close_utc(day),
            )
            decision = (
                apply_risk_gate(
                    proposal.weight_map(),
                    weights_now,
                    limits,
                    asset_class,
                    nav,
                    no_trade_band=no_trade_band,
                )
                if apply_gate
                else None
            )
            if decision is None:
                raise ResearchError("strategy path requires the research risk gate")
            nxt = calendar.next_trading_day(day)
            record_signal = nxt >= evaluate_start and day <= evaluate_end
            if record_signal:
                proposals.append(proposal.as_record())
                decisions.append(
                    {
                        "schema": "research-proposal-v1",
                        "record_type": "decision",
                        "proposal_id": proposal.proposal_id,
                        "signal_date": day.isoformat(),
                        "data_cutoff": proposal.data_cutoff.isoformat(),
                        "accepted": decision.accepted,
                        "veto": decision.veto,
                        "clipped": decision.clipped,
                        "weights": {ticker: format(weight, "f") for ticker, weight in decision.weights},
                        "cash_weight": format(decision.cash_weight, "f"),
                        "reasons": list(decision.reasons),
                    }
                )
            if (
                nxt <= evaluate_end
                and decision.accepted
                and _material_change(decision.weight_map(), weights_now, nav, limits.min_trade_notional_gbp)
            ):
                pending = (nxt, decision.weight_map(), day)
    if len(rows) < 2:
        raise ResearchError("the evaluation window does not contain two NAV rows")
    total_cost = sum((fill.total_cost_gbp for fill in fills), ZERO_MONEY)
    return tuple(rows), tuple(fills), tuple(proposals), tuple(decisions), total_cost, total_turnover


def simulate(
    *,
    instruments: Sequence[Instrument],
    bars: Mapping[str, Sequence[Bar]],
    calendar: WeekdayCalendar,
    strategy: Strategy,
    limits: ResearchLimits,
    cost_model: CostModel | None = None,
    starting_nav_gbp: Decimal | None = None,
    history_start: date | None = None,
    evaluate_start: date | None = None,
    evaluate_end: date | None = None,
    label: str = "research illustration",
    deduct_ocf: bool = False,
    include_benchmarks: bool = True,
    no_trade_band: Decimal = ZERO,
    cost_on: Callable[[date], CostModel] | None = None,
    fx_gbp_per_unit: Mapping[date, Decimal] | None = None,
    return_method: str = "auto",
) -> SimulationResult:
    assert_not_on_execution_path()
    if deduct_ocf:
        raise ResearchError(
            "Refusing to deduct OCF, TER, or an all-in fee from exchange prices. "
            "Those prices are already net of the charge. Use gross_index_fee_drag only on a labelled gross index."
        )
    if not instruments:
        raise ResearchError("a simulation needs at least one instrument")
    model = cost_model if cost_model is not None else CostModel()
    prepared: dict[str, PreparedSeries] = {}
    by_ticker: dict[str, Instrument] = {}
    caveats: list[str] = [OCF_NOTE]
    for instrument in instruments:
        if instrument.calendar_id != calendar.calendar_id:
            raise ResearchError(f"{instrument.ticker} calendar does not match the run calendar")
        by_ticker[instrument.ticker] = instrument
        series_bars = tuple(bars[instrument.ticker])
        prepared_series = prepare_series(
            ticker=instrument.ticker,
            distribution=instrument.distribution,
            trading_currency=instrument.trading_currency,
            bars=series_bars,
            calendar=calendar,
            fx_gbp_per_unit=fx_gbp_per_unit,
            return_method=return_method,
        )
        prepared[instrument.ticker] = prepared_series
        caveats.extend(prepared_series.caveats)
    first = prepared[instruments[0].ticker].bars[0].session
    last = prepared[instruments[0].ticker].bars[-1].session
    start_history = history_start or first
    start_eval = evaluate_start or start_history
    end_eval = evaluate_end or last
    starting = limits.starting_nav_gbp if starting_nav_gbp is None else starting_nav_gbp
    rows, fills, proposals, decisions, total_cost, turnover = _run_path(
        prepared=prepared,
        instruments=by_ticker,
        calendar=calendar,
        model=model,
        limits=limits,
        starting_nav=starting,
        history_start=start_history,
        evaluate_start=start_eval,
        evaluate_end=end_eval,
        strategy=strategy,
        static_weights=None,
        apply_gate=True,
        no_trade_band=no_trade_band,
        cost_on=cost_on,
    )
    benchmarks: list[tuple[str, Performance]] = []
    if include_benchmarks:
        benchmarks.extend(
            _benchmarks(
                prepared=prepared,
                instruments=instruments,
                by_ticker=by_ticker,
                calendar=calendar,
                model=model,
                limits=limits,
                cost_on=cost_on,
                starting=starting,
                start_eval=start_eval,
                end_eval=end_eval,
            )
        )
    metrics = performance(rows, total_cost_gbp=total_cost, total_turnover=turnover)
    return SimulationResult(
        mode=RESEARCH_MODE,
        execution_permission=EXECUTION_PERMISSION,
        orders_submitted=ORDERS_SUBMITTED,
        label=label,
        nav=rows,
        fills=fills,
        proposals=proposals,
        decisions=decisions,
        metrics=metrics,
        benchmarks=tuple(benchmarks),
        caveats=tuple(dict.fromkeys(caveats)),
        ocf_deducted_gbp=ZERO_MONEY,
        total_cost_gbp=total_cost,
        total_turnover=turnover,
    )


def _benchmarks(
    *,
    prepared: Mapping[str, PreparedSeries],
    instruments: Sequence[Instrument],
    by_ticker: Mapping[str, Instrument],
    calendar: WeekdayCalendar,
    model: CostModel,
    limits: ResearchLimits,
    starting: Decimal,
    start_eval: date,
    end_eval: date,
    cost_on: Callable[[date], CostModel] | None = None,
) -> list[tuple[str, Performance]]:
    benchmarks: list[tuple[str, Performance]] = []
    for instrument in instruments:
        static = {instrument.ticker: Decimal(1)}
        bench_rows, bench_fills, _p, _d, bench_cost, bench_turnover = _run_path(
            prepared=prepared,
            instruments=by_ticker,
            calendar=calendar,
            model=model,
            limits=limits,
            starting_nav=starting,
            history_start=start_eval,
            evaluate_start=start_eval,
            evaluate_end=end_eval,
            strategy=None,
            static_weights=static,
            apply_gate=False,
            cost_on=cost_on,
        )
        del bench_fills
        benchmarks.append(
            (
                f"buy_and_hold:{instrument.ticker}",
                performance(bench_rows, total_cost_gbp=bench_cost, total_turnover=bench_turnover),
            )
        )
    count = Decimal(len(instruments))
    each = quantize_weight_down(Decimal(1) / count)
    equal = {instrument.ticker: each for instrument in instruments}
    equal_rows, _fills, _p, _d, equal_cost, equal_turnover = _run_path(
        prepared=prepared,
        instruments=by_ticker,
        calendar=calendar,
        model=model,
        limits=limits,
        starting_nav=starting,
        history_start=start_eval,
        evaluate_start=start_eval,
        evaluate_end=end_eval,
        strategy=None,
        static_weights=equal,
        apply_gate=False,
        monthly_rebalance=True,
        cost_on=cost_on,
    )
    benchmarks.append(("equal_weight_monthly", performance(equal_rows, total_cost_gbp=equal_cost, total_turnover=equal_turnover)))
    return benchmarks

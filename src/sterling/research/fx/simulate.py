"""Unleveraged deposit book.

Foreign currency is bought with GBP at that morning's next available spot.
Cash that is not invested earns the lagged GBP retail deposit rate. Foreign
balances earn the lagged foreign retail rate. Gross exposure never exceeds the
NAV, weights are never negative, and the result cannot report an order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from sterling.research.errors import ResearchError, ResearchIsolationError
from sterling.research.fx.config import CostScenario, DepositScenario, FxResearchConfig
from sterling.research.fx.series import Market, StepSeries, month_end_dates
from sterling.research.fx.strategies import FxView, equal_weights
from sterling.research.isolation import assert_not_on_execution_path
from sterling.research.metrics import NavRow, Performance, performance
from sterling.research.money import ZERO

YEAR = Decimal(365)
DUST = Decimal("0.01")
EXECUTION_PERMISSION = "NOT_ISSUED"
ORDERS_SUBMITTED = 0


@dataclass(frozen=True, slots=True)
class Book:
    gbp: Decimal
    foreign: tuple[tuple[str, Decimal], ...]

    def units(self, code: str) -> Decimal:
        for name, amount in self.foreign:
            if name == code:
                return amount
        return ZERO


class WeightModel(Protocol):
    def target(self, view: FxView) -> Mapping[str, Decimal]:
        """Long-only weights. An empty map is 100% GBP cash."""
        ...


@dataclass(frozen=True, slots=True)
class FxResult:
    name: str
    group: str
    rows: tuple[NavRow, ...]
    performance: Performance
    trades: int
    one_way_turnover: Decimal
    total_cost_gbp: Decimal
    stale_foreign_days: tuple[tuple[str, int], ...]
    execution_permission: str = EXECUTION_PERMISSION
    orders_submitted: int = ORDERS_SUBMITTED
    mode: str = "research"

    def __post_init__(self) -> None:
        if self.execution_permission != EXECUTION_PERMISSION or self.orders_submitted != ORDERS_SUBMITTED:
            raise ResearchIsolationError("FX research cannot issue execution permission or submit orders")
        if self.mode != "research":
            raise ResearchIsolationError("FX research mode cannot be changed")


def apply_haircut(reference: Decimal, haircut: Decimal, *, floor_at_zero: bool) -> Decimal:
    rate = reference - haircut
    if floor_at_zero and rate < 0:
        return ZERO
    return rate


def decision_schedule(panel: Sequence[date], min_month_ends: int) -> tuple[tuple[date, date], ...]:
    """Pairs of ``(signal month-end, next panel day)`` once history is long enough."""
    if min_month_ends < 1:
        raise ResearchError("month-end lookback must be at least 1")
    ends = month_end_dates(list(panel))
    index = {day: position for position, day in enumerate(panel)}
    pairs: list[tuple[date, date]] = []
    for position, month_end in enumerate(ends):
        if position + 1 < min_month_ends:
            continue
        nxt = index[month_end] + 1
        if nxt >= len(panel):
            continue
        decision = panel[nxt]
        if month_end >= decision:
            raise ResearchError("signal month-end is not before the execution day")
        pairs.append((month_end, decision))
    if not pairs:
        raise ResearchError("no decision dates after the pre-registered lookback")
    return tuple(pairs)


def _retail(reference: Decimal | None, haircut: Decimal, scenario: DepositScenario) -> Decimal | None:
    if reference is None:
        return None
    return apply_haircut(reference, haircut, floor_at_zero=scenario.floor_at_zero)


def make_view(
    market: Market,
    *,
    month_end: date,
    decision: date,
    month_ends: Sequence[date],
    deposit: DepositScenario,
) -> FxView:
    if month_end >= decision:
        raise ResearchError("signal month-end must be strictly before the decision")
    usable = tuple(day for day in month_ends if day <= month_end)
    if any(day >= decision for day in usable):
        raise ResearchError("signal history includes the execution day")
    spot_maps = {code: market.spot_map(code) for code in market.codes}
    history: list[tuple[str, tuple[tuple[date, Decimal], ...]]] = []
    for code in market.codes:
        levels: list[tuple[date, Decimal]] = []
        for day in usable:
            levels.append((day, Decimal(1) / spot_maps[code][day]))
        history.append((code, tuple(levels)))
    gbp_series = market.sonia if deposit.gbp_reference == "sonia" else market.bank_rate
    gbp_known = gbp_series.as_of(decision)
    rates: list[tuple[str, Decimal | None]] = [
        ("GBP", _retail(None if gbp_known is None else gbp_known[1], deposit.gbp_haircut, deposit))
    ]
    for code in market.codes:
        if deposit.foreign_forced_rate is not None:
            rates.append((code, deposit.foreign_forced_rate))
            continue
        known = market.policy_series(code).as_of(decision)
        rates.append((code, _retail(None if known is None else known[1], deposit.foreign_haircut, deposit)))
    return FxView(decision=decision, month_end=month_end, gbp_per_unit=tuple(history), retail_rate=tuple(rates))


def _mark(book: Book, spots: Mapping[str, Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    foreign_value = ZERO
    for code, units in book.foreign:
        if units < 0:
            raise ResearchError(f"{code} balance went negative ({units})")
        foreign_value += units / spots[code]
    nav = book.gbp + foreign_value
    if nav <= 0:
        raise ResearchError("NAV must stay positive")
    if book.gbp < -Decimal("0.0000001"):
        raise ResearchError("GBP cash went negative")
    return nav, foreign_value, book.gbp


def _accrue_currency(series: StepSeries, day: date, haircut: Decimal, scenario: DepositScenario) -> tuple[Decimal, bool]:
    if scenario.foreign_forced_rate is not None:
        return scenario.foreign_forced_rate, False
    known = series.as_of(day)
    if known is None:
        return ZERO, True
    return apply_haircut(known[1], haircut, floor_at_zero=scenario.floor_at_zero), False


def _rebalance(
    book: Book,
    spots: Mapping[str, Decimal],
    targets: Mapping[str, Decimal],
    *,
    half_spreads: Mapping[str, Decimal],
    fee_one_way: Decimal,
    codes: Sequence[str],
) -> tuple[Book, Decimal, Decimal, int]:
    nav, _foreign_value, _cash = _mark(book, spots)
    if any(weight < 0 for weight in targets.values()):
        raise ResearchError("short selling is prohibited")
    gross = sum(targets.values(), ZERO)
    if gross > 1:
        raise ResearchError("gross exposure cannot exceed 1")
    for code in targets:
        if code not in spots:
            raise ResearchError(f"target {code} has no execution spot")
    gbp = book.gbp
    units = {code: book.units(code) for code in codes}
    cost = ZERO
    traded = ZERO
    trades = 0
    for code in codes:
        spot = spots[code]
        current = units[code] / spot
        target_value = targets.get(code, ZERO) * nav
        if current <= target_value + DUST:
            continue
        # Cap the sale at the units held. Decimal division does not round-trip,
        # so `current * spot` can be a hair more than the balance.
        fcy_sold = min(units[code], (current - target_value) * spot)
        if fcy_sold <= 0:
            continue
        reduce = fcy_sold / spot
        half = half_spreads[code]
        received = reduce / (Decimal(1) + half)
        fee = fee_one_way * reduce
        units[code] -= fcy_sold
        gbp += received - fee
        cost += (reduce - received) + fee
        traded += reduce
        trades += 1
    desired: dict[str, Decimal] = {}
    required = ZERO
    for code in codes:
        spot = spots[code]
        current = units[code] / spot
        target_value = targets.get(code, ZERO) * nav
        if target_value <= current + DUST:
            continue
        increase = target_value - current
        half = half_spreads[code]
        unit_cash = Decimal(1) / (Decimal(1) - half) + fee_one_way
        desired[code] = increase
        required += increase * unit_cash
    scale = Decimal(1)
    if required > gbp:
        scale = ZERO if gbp <= 0 or required <= 0 else gbp / required
    for code, increase in desired.items():
        increase *= scale
        if increase <= DUST:
            continue
        half = half_spreads[code]
        unit_cash = Decimal(1) / (Decimal(1) - half) + fee_one_way
        if increase * unit_cash > gbp:
            increase = gbp / unit_cash
        spend = increase / (Decimal(1) - half)
        fee = fee_one_way * increase
        units[code] += spend * spots[code] * (Decimal(1) - half)
        gbp -= spend + fee
        cost += (spend - increase) + fee
        traded += increase
        trades += 1
    if gbp < -Decimal("0.0000001"):
        raise ResearchError("rebalance drove GBP cash negative")
    if gbp < 0:
        gbp = ZERO
    updated = Book(gbp=gbp, foreign=tuple((code, units[code]) for code in codes))
    one_way = ZERO if nav <= 0 else traded / nav / Decimal(2)
    return updated, cost, one_way, trades


def run_path(
    *,
    market: Market,
    schedule: Sequence[tuple[date, date]],
    strategy: WeightModel | None,
    deposit: DepositScenario,
    cost: CostScenario,
    config: FxResearchConfig,
    mode: str,
    name: str,
    group: str,
) -> FxResult:
    assert_not_on_execution_path()
    if mode not in {"signal", "buy_and_hold", "cash"}:
        raise ResearchError("FX path mode must be signal, buy_and_hold, or cash")
    if mode == "signal" and strategy is None:
        raise ResearchError("a signal path needs a strategy")
    codes = market.codes
    spot_maps = {code: market.spot_map(code) for code in codes}
    panel = market.panel
    start = schedule[0][0]
    sessions = tuple(day for day in panel if day >= start)
    month_ends = month_end_dates(list(panel))
    decisions = {decision: month_end for month_end, decision in schedule}
    book = Book(gbp=config.starting_nav_gbp, foreign=tuple((code, ZERO) for code in codes))
    stale = {code: 0 for code in codes}
    total_cost = ZERO
    total_turnover = ZERO
    trades = 0
    held_once = False

    def spots_on(day: date) -> dict[str, Decimal]:
        return {code: spot_maps[code][day] for code in codes}

    def snap(day: date, current: Book) -> NavRow:
        prices = spots_on(day)
        nav, foreign_value, cash = _mark(current, prices)
        holdings = tuple((code, (current.units(code) / prices[code]) / nav) for code in codes if current.units(code) > 0)
        return NavRow(
            session=day,
            cash_gbp=cash,
            market_value_gbp=foreign_value,
            nav_gbp=nav,
            gross_exposure=foreign_value / nav,
            cash_weight=cash / nav,
            holdings=holdings,
        )

    rows: list[NavRow] = [snap(start, book)]
    previous = start
    for session in sessions:
        if session == start:
            continue
        gap = Decimal((session - previous).days)
        gbp_known = (market.sonia if deposit.gbp_reference == "sonia" else market.bank_rate).as_of(previous)
        if gbp_known is None:
            raise ResearchError(f"GBP deposit rate is unknown on {previous.isoformat()}")
        gbp_rate = apply_haircut(gbp_known[1], deposit.gbp_haircut, floor_at_zero=deposit.floor_at_zero)
        factor = Decimal(1) + gbp_rate * gap / YEAR
        units = {code: book.units(code) for code in codes}
        for code in codes:
            held = units[code] != 0
            rate, missing = _accrue_currency(
                market.policy_series(code),
                previous,
                deposit.foreign_haircut,
                deposit,
            )
            if missing and held:
                stale[code] += 1
            units[code] *= Decimal(1) + rate * gap / YEAR
        book = Book(gbp=book.gbp * factor, foreign=tuple((code, units[code]) for code in codes))
        rebalance_now = False
        targets: Mapping[str, Decimal] = {}
        if mode == "buy_and_hold" and session == schedule[0][1] and not held_once:
            targets = equal_weights(codes)
            held_once = True
            rebalance_now = True
        elif mode == "signal" and session in decisions and strategy is not None:
            view = make_view(
                market,
                month_end=decisions[session],
                decision=session,
                month_ends=month_ends,
                deposit=deposit,
            )
            targets = strategy.target(view)
            rebalance_now = True
        if rebalance_now:
            half = {code: config.half_spread(code, session, cost) for code in codes}
            book, spent, turnover, dealt = _rebalance(
                book,
                spots_on(session),
                targets,
                half_spreads=half,
                fee_one_way=cost.fee_one_way,
                codes=codes,
            )
            total_cost += spent
            total_turnover += turnover
            trades += dealt
        marked = snap(session, book)
        if marked.gross_exposure > 1 + Decimal("0.0000001"):
            raise ResearchError("gross exposure exceeded 1")
        rows.append(marked)
        previous = session
    path = performance(tuple(rows), total_cost_gbp=total_cost, total_turnover=total_turnover)
    return FxResult(
        name=name,
        group=group,
        rows=tuple(rows),
        performance=path,
        trades=trades,
        one_way_turnover=total_turnover,
        total_cost_gbp=total_cost,
        stale_foreign_days=tuple((code, stale[code]) for code in codes),
    )

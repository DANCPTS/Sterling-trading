"""Point-in-time daily-loss figures.

Day loss is max(0, day-opening equity - adjusted equity). It is not an
intra-day high-water mark and it is not a halt. A later recovery reduces the
reported figure. Stage 2 must latch a halt separately: once day loss reaches
the threshold, new entries stay blocked for the rest of that trading day even
if equity recovers. Deposits and withdrawals since the baseline are removed
from equity before the subtraction, so they do not reset the budget and a
withdrawal is not itself a trading loss.

Valuation rows are accounting labels, not execution quotes. Available cash and
exposure are ``UNKNOWN`` when a pending buy has no determinable GBP amount.
Stage 2 must fail closed on ``UNKNOWN``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sterling.ledger.allocation import Allocation, allocate
from sterling.ledger.calendar import london_date, require_aware
from sterling.ledger.engine import DayBaseline, _State, equity_of, market_value
from sterling.ledger.events import DepositEvent, LedgerEvent, WithdrawalEvent
from sterling.ledger.money import quantize_money
from sterling.ledger.reservations import Availability, summarise_availability
from sterling.ledger.valuation import PositionValuation, open_valuations
from sterling.rules.caps import daily_loss_threshold
from sterling.rules.model import DeskRules

_ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class DailyLossMetrics:
    london_date: date
    baseline_established: bool
    day_opening_equity_gbp: Decimal | None
    marked_equity_gbp: Decimal
    net_external_deposits_gbp: Decimal
    adjusted_equity_gbp: Decimal
    day_loss_gbp: Decimal
    daily_loss_threshold_gbp: Decimal | None
    daily_loss_threshold_reached: bool | None
    valuations: tuple[PositionValuation, ...]
    available_cash_gbp: Decimal | None
    available_cash_status: str
    gross_exposure_gbp: Decimal | None
    exposure_status: str


def daily_loss_metrics(
    state: _State,
    events: tuple[LedgerEvent, ...] | list[LedgerEvent],
    as_of: datetime,
    rules: DeskRules,
) -> DailyLossMetrics:
    """Metrics for the London day of ``as_of``, using only events at or before it.

    ``state`` must be the reduction of ``events``. Threshold reached is
    informational. Stage 1 does not halt.
    """
    require_aware(as_of)
    day = london_date(as_of)
    equity = equity_of(state)
    valuations = open_valuations(state, as_of)
    funds = _funds(state)
    baseline: DayBaseline | None = state.baselines.get(day)
    if baseline is None:
        return DailyLossMetrics(
            london_date=day,
            baseline_established=False,
            day_opening_equity_gbp=None,
            marked_equity_gbp=equity,
            net_external_deposits_gbp=_ZERO,
            adjusted_equity_gbp=equity,
            day_loss_gbp=_ZERO,
            daily_loss_threshold_gbp=None,
            daily_loss_threshold_reached=None,
            valuations=valuations,
            available_cash_gbp=funds.available_cash_gbp,
            available_cash_status=funds.available_cash_status,
            gross_exposure_gbp=funds.gross_exposure_gbp,
            exposure_status=funds.exposure_status,
        )
    net = _net_external_since_baseline(state, events, baseline)
    adjusted = quantize_money(equity - net)
    raw_loss = quantize_money(baseline.equity_gbp - adjusted)
    day_loss = raw_loss if raw_loss > 0 else _ZERO
    threshold = daily_loss_threshold(baseline.equity_gbp, rules)
    return DailyLossMetrics(
        london_date=day,
        baseline_established=True,
        day_opening_equity_gbp=baseline.equity_gbp,
        marked_equity_gbp=equity,
        net_external_deposits_gbp=net,
        adjusted_equity_gbp=adjusted,
        day_loss_gbp=day_loss,
        daily_loss_threshold_gbp=threshold,
        daily_loss_threshold_reached=day_loss >= threshold,
        valuations=valuations,
        available_cash_gbp=funds.available_cash_gbp,
        available_cash_status=funds.available_cash_status,
        gross_exposure_gbp=funds.gross_exposure_gbp,
        exposure_status=funds.exposure_status,
    )


def _funds(state: _State) -> Availability:
    settled = Decimal("0.00")
    for position in state.positions.values():
        settled = quantize_money(settled + market_value(position))
    pending_buys = [fill for fill in state.pending.values() if fill.side == "buy"]
    return summarise_availability(
        cash_gbp=state.cash,
        pending_buys=pending_buys,
        settled_market_value_gbp=settled,
    )


def fee_allocation(
    state: _State,
    events: tuple[LedgerEvent, ...] | list[LedgerEvent],
) -> Allocation:
    """Entry-fee pool and per-close nets. Does not change cash or equity."""
    return allocate(state, events)


def consecutive_net_closed_losses(
    state: _State,
    events: tuple[LedgerEvent, ...] | list[LedgerEvent],
) -> int:
    """Trailing run of closes whose net, after allocated fees, is negative.

    Nothing here halts, and a zero net does not change the streak.
    """
    return allocate(state, events).consecutive_net_closed_losses


def _net_external_since_baseline(
    state: _State,
    events: tuple[LedgerEvent, ...] | list[LedgerEvent],
    baseline: DayBaseline,
) -> Decimal:
    net = _ZERO
    for index, event in enumerate(events):
        if index <= baseline.event_index:
            continue
        if london_date(event.timestamp) != baseline.london_date:
            continue
        if event.event_id in state.reversed_ids:
            continue
        if isinstance(event, DepositEvent):
            net += event.amount_gbp
        elif isinstance(event, WithdrawalEvent):
            net -= event.amount_gbp
    return quantize_money(net)

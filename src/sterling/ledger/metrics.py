"""Point-in-time daily-loss figures.

Day loss is max(0, day-opening equity - adjusted equity). It is not an
intra-day high-water mark and it is not a halt. Deposits and withdrawals
since the baseline are removed from equity before the subtraction, so they
do not reset the budget and a withdrawal is not itself a trading loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sterling.ledger.calendar import london_date, require_aware
from sterling.ledger.engine import DayBaseline, _State, equity_of
from sterling.ledger.events import DepositEvent, FeeEvent, FxEvent, LedgerEvent, WithdrawalEvent
from sterling.ledger.money import quantize_money
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
    )


def consecutive_net_closed_losses(
    state: _State,
    events: tuple[LedgerEvent, ...] | list[LedgerEvent],
) -> int:
    """Count the trailing run of losing settled sells.

    Net means realised price P&L minus fees whose ``related_event_id`` is that
    sell's fill id or FX id. Buy-side fees are not allocated. A zero net does
    not change the streak. Reversed events are ignored. Nothing here halts.
    """
    fees_by_related: dict[str, Decimal] = {}
    for event in events:
        if event.event_id in state.reversed_ids or not isinstance(event, FeeEvent):
            continue
        if event.related_event_id is None:
            continue
        effect = state.effects[event.event_id]
        current = fees_by_related.get(event.related_event_id, _ZERO)
        fees_by_related[event.related_event_id] = quantize_money(current + effect.fee_delta)

    streak = 0
    for event in events:
        if not isinstance(event, FxEvent) or event.event_id in state.reversed_ids:
            continue
        effect = state.effects[event.event_id]
        if effect.side != "sell" or effect.fill_id is None:
            continue
        net = effect.realised_delta
        net -= fees_by_related.get(event.event_id, _ZERO)
        net -= fees_by_related.get(effect.fill_id, _ZERO)
        net = quantize_money(net)
        if net < 0:
            streak += 1
        elif net > 0:
            streak = 0
    return streak


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

"""Net round-trip results, including allocated entry fees.

Fees reduce cash once, and equity is cash plus market value, so this module
does not post another cash movement. It only attributes fees that have already
been recorded.

Allocation method
-----------------
Entry fees are commission, FX fee, and stamp duty/SDRT whose ``related_event_id``
is a buy fill or that buy's FX settlement. They sit in one average-cost pool per
instrument, together with the open quantity. They are not added to ``total_cost_gbp``.

A settled sell of ``q`` from pool quantity ``Q`` takes
``round_half_even(pool * q / Q)`` pence of entry fees. A sell that finishes the
position takes the exact remainder, so partial exits cannot leave a penny
stranded on a flat book. What is not taken stays with the open quantity.

Exit fees are those related to the sell fill or its FX settlement. They are
charged entirely to that close. A fee posted before the FX settlement waits on
the fill id and is applied when that fill settles. A fee posted afterwards
adjusts that close. An entry fee that arrives after the position is already
flat is applied to the latest close of that instrument. An entry fee for a fill
that never settles is not part of a close; it has already reduced equity
through cash.

A close's net is price P&L minus allocated entry fees minus exit fees. The loss
streak counts a negative net as a loss and a positive net as a reset. Zero
leaves the streak unchanged. Reversed events are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sterling.errors import LedgerRejected
from sterling.ledger.engine import _State
from sterling.ledger.events import FeeEvent, FillEvent, FxEvent, LedgerEvent
from sterling.ledger.money import quantize_money

_ZERO = Decimal("0.00")
_ZERO_QTY = Decimal("0")


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One settled sell and the fees attributed to that quantity."""

    fx_event_id: str
    fill_id: str
    instrument_id: str
    quantity: Decimal
    price_pnl_gbp: Decimal
    allocated_entry_fees_gbp: Decimal
    exit_fees_gbp: Decimal
    net_gbp: Decimal


@dataclass
class _Pool:
    quantity: Decimal = _ZERO_QTY
    entry_fees: Decimal = _ZERO


@dataclass
class _Close:
    fx_event_id: str
    fill_id: str
    instrument_id: str
    quantity: Decimal
    price_pnl_gbp: Decimal
    allocated_entry_fees_gbp: Decimal
    exit_fees_gbp: Decimal
    net_gbp: Decimal


@dataclass(frozen=True, slots=True)
class Allocation:
    trades: tuple[ClosedTrade, ...]
    unallocated_entry_fees_gbp: dict[str, Decimal]
    consecutive_net_closed_losses: int


def allocate(state: _State, events: tuple[LedgerEvent, ...] | list[LedgerEvent]) -> Allocation:
    """Replay ``events`` for fee attribution. ``state`` supplies amounts and reversals."""
    fills: dict[str, FillEvent] = {}
    fx_targets: dict[str, tuple[str, str, str]] = {}
    pools: dict[str, _Pool] = {}
    closes: list[_Close] = []
    closes_by_fill: dict[str, _Close] = {}
    settled_buys: set[str] = set()
    settled_sells: set[str] = set()
    pending_entry: dict[str, Decimal] = {}
    pending_exit: dict[str, Decimal] = {}

    def pool_for(instrument_id: str) -> _Pool:
        pool = pools.get(instrument_id)
        if pool is None:
            pool = _Pool()
            pools[instrument_id] = pool
        return pool

    def add_entry_fee(instrument_id: str, amount: Decimal) -> None:
        pool = pool_for(instrument_id)
        if pool.quantity > 0:
            pool.entry_fees = quantize_money(pool.entry_fees + amount)
            return
        for close in reversed(closes):
            if close.instrument_id == instrument_id:
                close.allocated_entry_fees_gbp = quantize_money(close.allocated_entry_fees_gbp + amount)
                close.net_gbp = quantize_money(close.net_gbp - amount)
                return
        pool.entry_fees = quantize_money(pool.entry_fees + amount)

    def add_exit_fee(fill_id: str, amount: Decimal) -> None:
        close = closes_by_fill[fill_id]
        close.exit_fees_gbp = quantize_money(close.exit_fees_gbp + amount)
        close.net_gbp = quantize_money(close.net_gbp - amount)

    for event in events:
        if event.event_id in state.reversed_ids:
            continue
        if isinstance(event, FillEvent):
            fills[event.event_id] = event
            continue
        if isinstance(event, FxEvent):
            fill = fills.get(event.fill_id)
            if fill is None:
                raise LedgerRejected(f"FX event {event.event_id} has no fill")
            effect = state.effects[event.event_id]
            fx_targets[event.event_id] = (fill.event_id, fill.side, fill.instrument_id)
            pool = pool_for(fill.instrument_id)
            if fill.side == "buy":
                pool.quantity += fill.quantity
                settled_buys.add(fill.event_id)
                waiting = pending_entry.pop(fill.event_id, _ZERO)
                if waiting != 0:
                    pool.entry_fees = quantize_money(pool.entry_fees + waiting)
                continue
            allocated = _entry_fee_share(pool.entry_fees, fill.quantity, pool.quantity)
            pool.entry_fees = quantize_money(pool.entry_fees - allocated)
            pool.quantity -= fill.quantity
            if pool.quantity == 0:
                pool.entry_fees = _ZERO
            exit_fees = pending_exit.pop(fill.event_id, _ZERO)
            price_pnl = effect.realised_delta
            close = _Close(
                fx_event_id=event.event_id,
                fill_id=fill.event_id,
                instrument_id=fill.instrument_id,
                quantity=fill.quantity,
                price_pnl_gbp=price_pnl,
                allocated_entry_fees_gbp=allocated,
                exit_fees_gbp=exit_fees,
                net_gbp=quantize_money(price_pnl - allocated - exit_fees),
            )
            closes.append(close)
            closes_by_fill[fill.event_id] = close
            settled_sells.add(fill.event_id)
            continue
        if isinstance(event, FeeEvent) and event.related_event_id is not None:
            target = _fee_target(event.related_event_id, fills, fx_targets)
            if target is None:
                continue
            role, fill_id, instrument_id = target
            amount = state.effects[event.event_id].fee_delta
            if role == "entry":
                if fill_id in settled_buys:
                    add_entry_fee(instrument_id, amount)
                else:
                    pending_entry[fill_id] = quantize_money(pending_entry.get(fill_id, _ZERO) + amount)
            elif fill_id in settled_sells:
                add_exit_fee(fill_id, amount)
            else:
                pending_exit[fill_id] = quantize_money(pending_exit.get(fill_id, _ZERO) + amount)

    trades = tuple(
        ClosedTrade(
            fx_event_id=close.fx_event_id,
            fill_id=close.fill_id,
            instrument_id=close.instrument_id,
            quantity=close.quantity,
            price_pnl_gbp=close.price_pnl_gbp,
            allocated_entry_fees_gbp=close.allocated_entry_fees_gbp,
            exit_fees_gbp=close.exit_fees_gbp,
            net_gbp=close.net_gbp,
        )
        for close in closes
    )
    unallocated = {
        instrument_id: pool.entry_fees for instrument_id, pool in pools.items() if pool.quantity > 0 and pool.entry_fees != 0
    }
    return Allocation(
        trades=trades,
        unallocated_entry_fees_gbp=unallocated,
        consecutive_net_closed_losses=_streak(trades),
    )


def _entry_fee_share(pool: Decimal, sold: Decimal, held: Decimal) -> Decimal:
    if held <= 0:
        raise LedgerRejected("entry-fee pool has no open quantity")
    if sold == held:
        return pool
    share = quantize_money(pool * sold / held)
    if share > pool:
        return pool
    return share


def _fee_target(
    related_event_id: str,
    fills: dict[str, FillEvent],
    fx_targets: dict[str, tuple[str, str, str]],
) -> tuple[str, str, str] | None:
    fill = fills.get(related_event_id)
    if fill is not None:
        role = "entry" if fill.side == "buy" else "exit"
        return role, fill.event_id, fill.instrument_id
    linked = fx_targets.get(related_event_id)
    if linked is None:
        return None
    fill_id, side, instrument_id = linked
    role = "entry" if side == "buy" else "exit"
    return role, fill_id, instrument_id


def _streak(trades: tuple[ClosedTrade, ...]) -> int:
    streak = 0
    for trade in trades:
        if trade.net_gbp < 0:
            streak += 1
        elif trade.net_gbp > 0:
            streak = 0
    return streak

"""Net round-trip results. Fees stay on the fill that incurred them.

Fees reduce cash once. Equity is cash plus market value, so this module does
not post cash. It only attributes fees already recorded.

Fill identity through average cost
----------------------------------
Each settled buy is its own lot: original quantity, remaining quantity, and
the entry fees still attached to the unsold units (commission, FX fee, stamp
duty/SDRT related to that fill or its FX settlement). Price inventory stays
on the instrument's average-cost pool. When a sell of quantity ``q`` meets
open quantity ``Q``, each open lot is consumed pro rata,
``consumed_i = remaining_i * q / Q``, quantized to 8 decimal places, with the
last lot taking the residual so the consumed quantities sum to ``q``. A sell
of the whole position consumes each lot's remaining quantity exactly.

Entry fees already on a lot are relieved in the same proportion. A full
consumption of that lot takes its remaining fee pence exactly. The relieved
fees are added to that sell's close.

A fee that arrives later is applied only to its own lot:

- nothing sold yet: the whole fee stays on the open units;
- partly sold: ``round(fee * closed / original)`` is split across the closes
  that consumed that lot, in proportion to the quantity each close took, and
  the rest stays on the still-open units;
- fully closed: the whole fee is split across those same closes.

A later buy, or a later close of a later buy, does not receive that fee.
Reversed events are skipped, so the streak is the same on replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal

from sterling.errors import LedgerRejected
from sterling.ledger.engine import _State
from sterling.ledger.events import FeeEvent, FillEvent, FxEvent, LedgerEvent
from sterling.ledger.money import quantize_money

_ZERO = Decimal("0.00")
_QTY = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One settled sell and the fees attributed to the lots it consumed."""

    fx_event_id: str
    fill_id: str
    instrument_id: str
    quantity: Decimal
    price_pnl_gbp: Decimal
    allocated_entry_fees_gbp: Decimal
    exit_fees_gbp: Decimal
    net_gbp: Decimal


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


@dataclass
class _Lot:
    fill_id: str
    instrument_id: str
    original_qty: Decimal
    remaining_qty: Decimal
    unallocated_fees: Decimal = _ZERO
    consumptions: list[tuple[_Close, Decimal]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Allocation:
    trades: tuple[ClosedTrade, ...]
    unallocated_entry_fees_gbp: dict[str, Decimal]
    consecutive_net_closed_losses: int


def allocate(state: _State, events: tuple[LedgerEvent, ...] | list[LedgerEvent]) -> Allocation:
    """Replay ``events`` for fee attribution. ``state`` supplies amounts and reversals."""
    fills: dict[str, FillEvent] = {}
    fx_targets: dict[str, tuple[str, str, str]] = {}
    lots: dict[str, _Lot] = {}
    lot_order: dict[str, list[_Lot]] = {}
    closes: list[_Close] = []
    closes_by_fill: dict[str, _Close] = {}
    settled_buys: set[str] = set()
    settled_sells: set[str] = set()
    pending_entry: dict[str, Decimal] = {}
    pending_exit: dict[str, Decimal] = {}

    def add_entry_fee(fill_id: str, amount: Decimal) -> None:
        lot = lots[fill_id]
        closed_qty = lot.original_qty - lot.remaining_qty
        if closed_qty == 0:
            lot.unallocated_fees = quantize_money(lot.unallocated_fees + amount)
            return
        if lot.remaining_qty == 0:
            _charge_closes(lot, amount)
            return
        to_closed = quantize_money(amount * closed_qty / lot.original_qty)
        if to_closed > amount:
            to_closed = amount
        to_open = quantize_money(amount - to_closed)
        lot.unallocated_fees = quantize_money(lot.unallocated_fees + to_open)
        if to_closed > 0:
            _charge_closes(lot, to_closed)

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
            if fill.side == "buy":
                lot = _Lot(
                    fill_id=fill.event_id,
                    instrument_id=fill.instrument_id,
                    original_qty=fill.quantity,
                    remaining_qty=fill.quantity,
                )
                lots[fill.event_id] = lot
                lot_order.setdefault(fill.instrument_id, []).append(lot)
                settled_buys.add(fill.event_id)
                waiting = pending_entry.pop(fill.event_id, _ZERO)
                if waiting != 0:
                    lot.unallocated_fees = waiting
                continue
            open_lots = [lot for lot in lot_order.get(fill.instrument_id, []) if lot.remaining_qty > 0]
            held = sum((lot.remaining_qty for lot in open_lots), Decimal("0"))
            parts = _pro_rata(open_lots, fill.quantity, held)
            fee_total = _ZERO
            for lot, consumed in parts:
                taken = _relieve_fee(lot, consumed)
                fee_total = quantize_money(fee_total + taken)
            exit_fees = pending_exit.pop(fill.event_id, _ZERO)
            price_pnl = effect.realised_delta
            close = _Close(
                fx_event_id=event.event_id,
                fill_id=fill.event_id,
                instrument_id=fill.instrument_id,
                quantity=fill.quantity,
                price_pnl_gbp=price_pnl,
                allocated_entry_fees_gbp=fee_total,
                exit_fees_gbp=exit_fees,
                net_gbp=quantize_money(price_pnl - fee_total - exit_fees),
            )
            closes.append(close)
            closes_by_fill[fill.event_id] = close
            settled_sells.add(fill.event_id)
            for lot, consumed in parts:
                if consumed > 0:
                    lot.consumptions.append((close, consumed))
            continue
        if isinstance(event, FeeEvent) and event.related_event_id is not None:
            target = _fee_target(event.related_event_id, fills, fx_targets)
            if target is None:
                continue
            role, fill_id, _instrument_id = target
            amount = state.effects[event.event_id].fee_delta
            if role == "entry":
                if fill_id in settled_buys:
                    add_entry_fee(fill_id, amount)
                else:
                    pending_entry[fill_id] = quantize_money(pending_entry.get(fill_id, _ZERO) + amount)
            elif fill_id in settled_sells:
                _add_exit_fee(closes_by_fill[fill_id], amount)
            else:
                pending_exit[fill_id] = quantize_money(pending_exit.get(fill_id, _ZERO) + amount)

    trades = tuple(_freeze(close) for close in closes)
    unallocated: dict[str, Decimal] = {}
    for lot in lots.values():
        if lot.remaining_qty > 0 and lot.unallocated_fees != 0:
            current = unallocated.get(lot.instrument_id, _ZERO)
            unallocated[lot.instrument_id] = quantize_money(current + lot.unallocated_fees)
    return Allocation(
        trades=trades,
        unallocated_entry_fees_gbp=unallocated,
        consecutive_net_closed_losses=_streak(trades),
    )


def _pro_rata(lots: list[_Lot], sold: Decimal, held: Decimal) -> list[tuple[_Lot, Decimal]]:
    if held <= 0:
        raise LedgerRejected("entry-fee lots have no open quantity")
    if sold > held:
        raise LedgerRejected("sell quantity exceeds open lots")
    if len(lots) == 1:
        return [(lots[0], sold)]
    if sold == held:
        return [(lot, lot.remaining_qty) for lot in lots]
    assigned = Decimal("0")
    parts: list[tuple[_Lot, Decimal]] = []
    for index, lot in enumerate(lots):
        if index == len(lots) - 1:
            share = sold - assigned
        else:
            share = (lot.remaining_qty * sold / held).quantize(_QTY, rounding=ROUND_HALF_EVEN)
            if share < 0:
                share = Decimal("0")
            if share > lot.remaining_qty:
                share = lot.remaining_qty
            if assigned + share > sold:
                share = sold - assigned
            assigned += share
        if share < 0 or share > lot.remaining_qty:
            raise LedgerRejected("pro-rata lot consumption is inconsistent")
        parts.append((lot, share))
    return parts


def _relieve_fee(lot: _Lot, consumed: Decimal) -> Decimal:
    if consumed == lot.remaining_qty:
        taken = lot.unallocated_fees
    else:
        taken = quantize_money(lot.unallocated_fees * consumed / lot.remaining_qty)
        if taken > lot.unallocated_fees:
            taken = lot.unallocated_fees
    lot.unallocated_fees = quantize_money(lot.unallocated_fees - taken)
    lot.remaining_qty -= consumed
    return taken


def _charge_closes(lot: _Lot, amount: Decimal) -> None:
    consumptions = [(close, qty) for close, qty in lot.consumptions if qty > 0]
    total = sum((qty for _, qty in consumptions), Decimal("0"))
    if total == 0:
        raise LedgerRejected(f"fill {lot.fill_id} has closed quantity but no consuming sell")
    remaining = amount
    for index, (close, qty) in enumerate(consumptions):
        if index == len(consumptions) - 1:
            share = remaining
        else:
            share = quantize_money(amount * qty / total)
            if share > remaining:
                share = remaining
            remaining = quantize_money(remaining - share)
        close.allocated_entry_fees_gbp = quantize_money(close.allocated_entry_fees_gbp + share)
        close.net_gbp = quantize_money(close.net_gbp - share)


def _add_exit_fee(close: _Close, amount: Decimal) -> None:
    close.exit_fees_gbp = quantize_money(close.exit_fees_gbp + amount)
    close.net_gbp = quantize_money(close.net_gbp - amount)


def _freeze(close: _Close) -> ClosedTrade:
    return ClosedTrade(
        fx_event_id=close.fx_event_id,
        fill_id=close.fill_id,
        instrument_id=close.instrument_id,
        quantity=close.quantity,
        price_pnl_gbp=close.price_pnl_gbp,
        allocated_entry_fees_gbp=close.allocated_entry_fees_gbp,
        exit_fees_gbp=close.exit_fees_gbp,
        net_gbp=close.net_gbp,
    )


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

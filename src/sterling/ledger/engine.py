"""Deterministic reducer for the GBP ledger.

Fills sit pending until an FX event settles them at a caller-supplied rate.
That rate is the only FX conversion of the consideration, so it is counted once.
Fees are separate cost lines and also hit cash once. Average cost is kept in
GBP. A full close relieves the remaining cost exactly so rounding does not
leave a residual.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sterling.errors import (
    IdempotencyConflict,
    InvalidEvent,
    LedgerRejected,
    MoneyError,
    NegativeCashRejected,
    OutOfOrderTimestamp,
    OversellRejected,
    ReversalRejected,
)
from sterling.ledger.calendar import london_date
from sterling.ledger.events import (
    DepositEvent,
    EquitySnapshotEvent,
    FeeEvent,
    FillEvent,
    FillVoidEvent,
    FxEvent,
    LedgerEvent,
    MarkEvent,
    ReversalEvent,
    WithdrawalEvent,
)
from sterling.ledger.money import gbp_notional, quantize_money, require_money
from sterling.ledger.reservations import buy_reservation_gbp

_ZERO = Decimal("0.00")
_ZERO_QTY = Decimal("0")


@dataclass(frozen=True, slots=True)
class Position:
    instrument_id: str
    currency: str
    quantity: Decimal
    total_cost_gbp: Decimal
    mark_price: Decimal | None = None
    mark_fx_to_gbp: Decimal | None = None
    mark_event_id: str | None = None
    mark_timestamp: datetime | None = None
    mark_source: str | None = None

    def replace(
        self,
        *,
        quantity: Decimal | None = None,
        total_cost_gbp: Decimal | None = None,
        mark_price: Decimal | None = None,
        mark_fx_to_gbp: Decimal | None = None,
        mark_event_id: str | None = None,
        mark_timestamp: datetime | None = None,
        mark_source: str | None = None,
        clear_mark: bool = False,
    ) -> Position:
        return Position(
            instrument_id=self.instrument_id,
            currency=self.currency,
            quantity=self.quantity if quantity is None else quantity,
            total_cost_gbp=self.total_cost_gbp if total_cost_gbp is None else total_cost_gbp,
            mark_price=None if clear_mark else (self.mark_price if mark_price is None else mark_price),
            mark_fx_to_gbp=(None if clear_mark else (self.mark_fx_to_gbp if mark_fx_to_gbp is None else mark_fx_to_gbp)),
            mark_event_id=(None if clear_mark else (self.mark_event_id if mark_event_id is None else mark_event_id)),
            mark_timestamp=None if clear_mark else (self.mark_timestamp if mark_timestamp is None else mark_timestamp),
            mark_source=None if clear_mark else (self.mark_source if mark_source is None else mark_source),
        )


@dataclass(frozen=True, slots=True)
class DayBaseline:
    london_date: date
    equity_gbp: Decimal
    as_of: datetime
    event_index: int


@dataclass(frozen=True, slots=True)
class Effect:
    kind: str
    cash_delta: Decimal = _ZERO
    realised_delta: Decimal = _ZERO
    instrument_id: str | None = None
    currency: str | None = None
    quantity_delta: Decimal = _ZERO_QTY
    cost_delta: Decimal = _ZERO
    fee_type: str | None = None
    fee_delta: Decimal = _ZERO
    side: str | None = None
    fill_id: str | None = None
    had_mark: bool = False
    prior_mark_price: Decimal | None = None
    prior_mark_fx: Decimal | None = None
    prior_mark_event_id: str | None = None
    prior_mark_timestamp: datetime | None = None
    prior_mark_source: str | None = None


class _State:
    __slots__ = (
        "baselines",
        "cash",
        "effects",
        "fees",
        "pending",
        "positions",
        "realised",
        "reversed_ids",
        "settled_fills",
        "settlement_order",
    )

    def __init__(self, cash: Decimal) -> None:
        self.cash = cash
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, FillEvent] = {}
        self.settled_fills: dict[str, FillEvent] = {}
        self.realised = _ZERO
        self.fees = {
            "commission": _ZERO,
            "fx_fee": _ZERO,
            "stamp_duty": _ZERO,
        }
        self.baselines: dict[date, DayBaseline] = {}
        self.effects: dict[str, Effect] = {}
        self.reversed_ids: set[str] = set()
        self.settlement_order: dict[str, list[str]] = {}

    def clone(self) -> _State:
        other = _State(self.cash)
        other.positions = dict(self.positions)
        other.pending = dict(self.pending)
        other.settled_fills = dict(self.settled_fills)
        other.realised = self.realised
        other.fees = dict(self.fees)
        other.baselines = dict(self.baselines)
        other.effects = dict(self.effects)
        other.reversed_ids = set(self.reversed_ids)
        other.settlement_order = {key: list(value) for key, value in self.settlement_order.items()}
        return other


def market_value(position: Position) -> Decimal:
    if position.quantity == 0:
        return _ZERO
    if position.mark_price is None or position.mark_fx_to_gbp is None:
        return position.total_cost_gbp
    return quantize_money(position.quantity * position.mark_price * position.mark_fx_to_gbp)


def equity_of(state: _State) -> Decimal:
    total = state.cash
    for position in state.positions.values():
        total += market_value(position)
    return quantize_money(total)


def reduce_events(initial_cash: Decimal, events: tuple[LedgerEvent, ...] | list[LedgerEvent]) -> _State:
    state = _State(require_money(initial_cash, label="initial_cash_gbp"))
    if state.cash < 0:
        raise NegativeCashRejected("initial cash cannot be negative")
    seen: set[str] = set()
    last: datetime | None = None
    for index, event in enumerate(events):
        if event.event_id in seen:
            raise IdempotencyConflict(f"duplicate event id {event.event_id}")
        seen.add(event.event_id)
        if last is not None and event.timestamp < last:
            raise OutOfOrderTimestamp("event timestamps must be non-decreasing")
        last = event.timestamp
        state = apply(state, event, index)
    return state


def apply(state: _State, event: LedgerEvent, index: int) -> _State:
    new = state.clone()
    if event.event_id in new.effects:
        raise IdempotencyConflict(f"duplicate event id {event.event_id}")
    _dispatch(new, event, index)
    _assert_invariants(new)
    return new


def _dispatch(state: _State, event: LedgerEvent, index: int) -> None:
    if isinstance(event, DepositEvent):
        _apply_deposit(state, event)
    elif isinstance(event, WithdrawalEvent):
        _apply_withdrawal(state, event)
    elif isinstance(event, FillEvent):
        _apply_fill(state, event)
    elif isinstance(event, FxEvent):
        _apply_fx(state, event)
    elif isinstance(event, FeeEvent):
        _apply_fee(state, event)
    elif isinstance(event, MarkEvent):
        _apply_mark(state, event)
    elif isinstance(event, ReversalEvent):
        _apply_reversal(state, event)
    elif isinstance(event, FillVoidEvent):
        _apply_fill_void(state, event)
    elif isinstance(event, EquitySnapshotEvent):
        _apply_snapshot(state, event, index)
    else:
        raise InvalidEvent("unsupported event")


def _apply_deposit(state: _State, event: DepositEvent) -> None:
    state.cash = quantize_money(state.cash + event.amount_gbp)
    state.effects[event.event_id] = Effect(kind="deposit", cash_delta=event.amount_gbp)


def _apply_withdrawal(state: _State, event: WithdrawalEvent) -> None:
    _require_spendable(state, event.amount_gbp, "withdrawal")
    if state.cash < event.amount_gbp:
        raise NegativeCashRejected("withdrawal exceeds GBP cash")
    state.cash = quantize_money(state.cash - event.amount_gbp)
    state.effects[event.event_id] = Effect(kind="withdrawal", cash_delta=-event.amount_gbp)


def _apply_fill(state: _State, event: FillEvent) -> None:
    if event.event_id in state.pending or event.event_id in state.settled_fills:
        raise InvalidEvent("fill id already used")
    if event.event_id in _voided_fill_ids(state):
        raise InvalidEvent("fill id was voided")
    _require_currency_match(state, event.instrument_id, event.instrument_currency)
    if event.side == "sell":
        _reserve_sell_quantity(state, event)
    else:
        _reserve_buy_cash(state, event)
    state.pending[event.event_id] = event
    state.effects[event.event_id] = Effect(
        kind="fill",
        instrument_id=event.instrument_id,
        currency=event.instrument_currency,
        side=event.side,
        fill_id=event.event_id,
    )


def _apply_fx(state: _State, event: FxEvent) -> None:
    if event.fill_id in state.settled_fills:
        raise InvalidEvent(f"fill {event.fill_id} is already settled")
    fill = state.pending.get(event.fill_id)
    if fill is None:
        raise InvalidEvent(f"fill {event.fill_id} is not pending")
    if fill.instrument_currency == "GBP" and event.fx_rate_to_gbp != Decimal("1"):
        raise InvalidEvent("GBP instruments require fx_rate_to_gbp of 1")
    if fill.fx_rate_ceiling is not None and event.fx_rate_to_gbp > fill.fx_rate_ceiling:
        raise InvalidEvent("fx rate exceeds the reserved ceiling")
    notional = gbp_notional(fill.quantity, fill.price, event.fx_rate_to_gbp)
    if notional <= 0:
        raise InvalidEvent("GBP notional rounds to zero; refusing a zero-value settlement")
    position = state.positions.get(fill.instrument_id)
    if position is None:
        if fill.side == "sell":
            raise OversellRejected("cannot sell an instrument that is not held")
        position = Position(
            instrument_id=fill.instrument_id,
            currency=fill.instrument_currency,
            quantity=_ZERO_QTY,
            total_cost_gbp=_ZERO,
        )
    elif position.currency != fill.instrument_currency:
        raise InvalidEvent("instrument currency does not match the open position")

    if fill.side == "buy":
        _require_spendable(state, notional, "buy settlement", exclude_fill_id=fill.event_id)
        if state.cash < notional:
            raise NegativeCashRejected("buy settlement exceeds GBP cash")
        updated = position.replace(
            quantity=position.quantity + fill.quantity,
            total_cost_gbp=quantize_money(position.total_cost_gbp + notional),
        )
        state.cash = quantize_money(state.cash - notional)
        effect = Effect(
            kind="fx",
            cash_delta=-notional,
            instrument_id=fill.instrument_id,
            currency=fill.instrument_currency,
            quantity_delta=fill.quantity,
            cost_delta=notional,
            side="buy",
            fill_id=fill.event_id,
        )
    else:
        if position.quantity < fill.quantity:
            raise OversellRejected("sell quantity exceeds the settled long position")
        if position.quantity == fill.quantity:
            cost = position.total_cost_gbp
        else:
            cost = quantize_money(position.total_cost_gbp * fill.quantity / position.quantity)
            if cost > position.total_cost_gbp:
                cost = position.total_cost_gbp
        new_quantity = position.quantity - fill.quantity
        new_cost = _ZERO if new_quantity == 0 else quantize_money(position.total_cost_gbp - cost)
        realised = quantize_money(notional - cost)
        updated = position.replace(quantity=new_quantity, total_cost_gbp=new_cost)
        state.cash = quantize_money(state.cash + notional)
        state.realised = quantize_money(state.realised + realised)
        effect = Effect(
            kind="fx",
            cash_delta=notional,
            realised_delta=realised,
            instrument_id=fill.instrument_id,
            currency=fill.instrument_currency,
            quantity_delta=-fill.quantity,
            cost_delta=-cost,
            side="sell",
            fill_id=fill.event_id,
        )
    state.positions[fill.instrument_id] = updated
    del state.pending[fill.event_id]
    state.settled_fills[fill.event_id] = fill
    state.settlement_order.setdefault(fill.instrument_id, []).append(event.event_id)
    state.effects[event.event_id] = effect


def _apply_fee(state: _State, event: FeeEvent) -> None:
    if event.currency == "GBP":
        try:
            gbp = require_money(event.amount, label="fee amount")
        except MoneyError as exc:
            raise InvalidEvent("GBP fee amount must already be in pence") from exc
    else:
        gbp = quantize_money(event.amount * event.fx_rate_to_gbp)
    if gbp <= 0:
        raise InvalidEvent("fee rounds to zero GBP")
    _require_spendable(state, gbp, "fee")
    if state.cash < gbp:
        raise NegativeCashRejected("fee exceeds GBP cash")
    if event.related_event_id is not None:
        if event.related_event_id not in state.effects:
            raise InvalidEvent("related_event_id does not match a prior event")
        if state.effects[event.related_event_id].kind not in ("fill", "fx"):
            raise InvalidEvent("related_event_id must reference a fill or its FX settlement")
    state.cash = quantize_money(state.cash - gbp)
    state.fees[event.fee_type] = quantize_money(state.fees[event.fee_type] + gbp)
    state.effects[event.event_id] = Effect(
        kind="fee",
        cash_delta=-gbp,
        fee_type=event.fee_type,
        fee_delta=gbp,
    )


def _apply_mark(state: _State, event: MarkEvent) -> None:
    position = state.positions.get(event.instrument_id)
    if position is None or position.quantity == 0:
        raise InvalidEvent("cannot mark an instrument with no settled quantity")
    if position.currency == "GBP" and event.fx_rate_to_gbp != Decimal("1"):
        raise InvalidEvent("GBP instruments require fx_rate_to_gbp of 1")
    state.effects[event.event_id] = Effect(
        kind="mark",
        instrument_id=position.instrument_id,
        had_mark=position.mark_price is not None,
        prior_mark_price=position.mark_price,
        prior_mark_fx=position.mark_fx_to_gbp,
        prior_mark_event_id=position.mark_event_id,
        prior_mark_timestamp=position.mark_timestamp,
        prior_mark_source=position.mark_source,
    )
    state.positions[position.instrument_id] = position.replace(
        mark_price=event.price,
        mark_fx_to_gbp=event.fx_rate_to_gbp,
        mark_event_id=event.event_id,
        mark_timestamp=event.timestamp,
        mark_source=event.source,
    )


def _apply_snapshot(state: _State, event: EquitySnapshotEvent, index: int) -> None:
    day = london_date(event.timestamp)
    if event.event_id != f"day-baseline-{day.isoformat()}":
        raise InvalidEvent("snapshot id does not match its Europe/London date")
    if day in state.baselines:
        raise InvalidEvent("baseline already captured for this Europe/London day")
    _assert_invariants(state)
    state.baselines[day] = DayBaseline(
        london_date=day,
        equity_gbp=equity_of(state),
        as_of=event.timestamp,
        event_index=index,
    )
    state.effects[event.event_id] = Effect(kind="equity_snapshot")


def _apply_reversal(state: _State, event: ReversalEvent) -> None:
    target_id = event.reverses_event_id
    effect = state.effects.get(target_id)
    if effect is None:
        raise ReversalRejected("reversal target does not exist")
    if target_id in state.reversed_ids:
        raise ReversalRejected("event has already been reversed")
    if effect.kind in ("reversal", "equity_snapshot"):
        raise ReversalRejected(f"cannot reverse a {effect.kind}")
    if effect.kind == "deposit":
        _cash_add(state, -effect.cash_delta, "reversing the deposit would make cash negative")
    elif effect.kind == "withdrawal":
        _cash_add(state, -effect.cash_delta, "reversing the withdrawal would make cash negative")
    elif effect.kind == "fee":
        _reverse_fee(state, effect)
    elif effect.kind == "fill":
        _reverse_fill(state, target_id)
    elif effect.kind == "fx":
        if effect.side == "buy" and _later_sell_depends_on_buy(state, effect.instrument_id, target_id):
            raise ReversalRejected("a later sell on this instrument depends on this buy; reversal refused")
        _reverse_fx(state, effect)
    elif effect.kind == "mark":
        _reverse_mark(state, target_id, effect)
    else:
        raise ReversalRejected(f"cannot reverse {effect.kind}")
    state.reversed_ids.add(target_id)
    state.effects[event.event_id] = Effect(
        kind="reversal",
        cash_delta=-effect.cash_delta,
        realised_delta=-effect.realised_delta,
    )


def _reverse_fee(state: _State, effect: Effect) -> None:
    if effect.fee_type is None:
        raise ReversalRejected("fee effect has no type")
    _cash_add(state, -effect.cash_delta, "reversing the fee would make cash negative")
    updated = quantize_money(state.fees[effect.fee_type] - effect.fee_delta)
    if updated < 0:
        raise ReversalRejected("fee total would become negative")
    state.fees[effect.fee_type] = updated


def _reverse_fill(state: _State, fill_id: str) -> None:
    if fill_id in state.settled_fills:
        raise ReversalRejected("reverse the FX settlement before reversing the fill")
    if fill_id not in state.pending:
        raise ReversalRejected("fill is not pending")
    del state.pending[fill_id]


def _reverse_fx(state: _State, effect: Effect) -> None:
    if effect.fill_id is None or effect.instrument_id is None:
        raise ReversalRejected("FX effect is incomplete")
    fill = state.settled_fills.get(effect.fill_id)
    position = state.positions.get(effect.instrument_id)
    if fill is None or position is None:
        raise ReversalRejected("settled fill cannot be restored")
    new_quantity = position.quantity - effect.quantity_delta
    new_cost = quantize_money(position.total_cost_gbp - effect.cost_delta)
    if new_quantity < 0:
        raise ReversalRejected("reversing the settlement would make quantity negative")
    if new_cost < 0:
        raise ReversalRejected("reversing the settlement would make cost negative")
    if new_quantity == 0:
        new_cost = _ZERO
    elif new_cost == 0:
        raise ReversalRejected("reversing the settlement would leave shares with no cost")
    _cash_add(state, -effect.cash_delta, "reversing the settlement would make cash negative")
    state.realised = quantize_money(state.realised - effect.realised_delta)
    state.positions[effect.instrument_id] = position.replace(
        quantity=new_quantity,
        total_cost_gbp=new_cost,
    )
    del state.settled_fills[effect.fill_id]
    state.pending[effect.fill_id] = fill


def _reverse_mark(state: _State, target_id: str, effect: Effect) -> None:
    if effect.instrument_id is None:
        raise ReversalRejected("mark effect has no instrument")
    position = state.positions.get(effect.instrument_id)
    if position is None or position.mark_event_id != target_id:
        raise ReversalRejected("only the latest mark on an instrument can be reversed")
    if effect.had_mark:
        state.positions[effect.instrument_id] = position.replace(
            mark_price=effect.prior_mark_price,
            mark_fx_to_gbp=effect.prior_mark_fx,
            mark_event_id=effect.prior_mark_event_id,
            mark_timestamp=effect.prior_mark_timestamp,
            mark_source=effect.prior_mark_source,
        )
    else:
        state.positions[effect.instrument_id] = position.replace(clear_mark=True)


def _apply_fill_void(state: _State, event: FillVoidEvent) -> None:
    if event.fill_id not in state.pending:
        raise InvalidEvent("fill is not pending")
    del state.pending[event.fill_id]
    state.effects[event.event_id] = Effect(kind="fill_void", fill_id=event.fill_id)


def _reserve_sell_quantity(state: _State, event: FillEvent) -> None:
    position = state.positions.get(event.instrument_id)
    settled = position.quantity if position is not None else Decimal("0")
    reserved = Decimal("0")
    for pending in state.pending.values():
        if pending.side == "sell" and pending.instrument_id == event.instrument_id:
            reserved += pending.quantity
    available = settled - reserved
    if event.quantity > available:
        raise OversellRejected("sell quantity exceeds settled long quantity net of pending sells")


def _reserve_buy_cash(state: _State, event: FillEvent) -> None:
    reservation = buy_reservation_gbp(event)
    known, unknown = _other_buy_reservations(state, exclude_fill_id=None)
    if reservation is None:
        if unknown or known > 0:
            raise InvalidEvent("available cash is UNKNOWN or already reserved; pending buy rejected")
        return
    if reservation <= 0:
        raise InvalidEvent("GBP notional rounds to zero; refusing a zero-value fill")
    if unknown:
        raise InvalidEvent("available cash is UNKNOWN; pending buy rejected")
    if quantize_money(known + reservation) > state.cash:
        raise NegativeCashRejected("pending buy reservation exceeds available cash")


def _other_buy_reservations(state: _State, exclude_fill_id: str | None) -> tuple[Decimal, bool]:
    total = Decimal("0.00")
    unknown = False
    for pending in state.pending.values():
        if pending.side != "buy" or pending.event_id == exclude_fill_id:
            continue
        amount = buy_reservation_gbp(pending)
        if amount is None:
            unknown = True
        else:
            total = quantize_money(total + amount)
    return total, unknown


def _require_spendable(state: _State, amount: Decimal, label: str, exclude_fill_id: str | None = None) -> None:
    reserved, unknown = _other_buy_reservations(state, exclude_fill_id)
    if unknown:
        raise InvalidEvent(f"available cash is UNKNOWN; {label} rejected")
    if quantize_money(state.cash - amount) < reserved:
        raise NegativeCashRejected(f"{label} would consume cash reserved for a pending buy")


def _later_sell_depends_on_buy(state: _State, instrument_id: str | None, buy_fx_id: str) -> bool:
    if instrument_id is None:
        return False
    order = state.settlement_order.get(instrument_id, [])
    if buy_fx_id not in order:
        return False
    start = order.index(buy_fx_id) + 1
    for later_id in order[start:]:
        if later_id in state.reversed_ids:
            continue
        later = state.effects.get(later_id)
        if later is not None and later.side == "sell":
            return True
    return False


def _voided_fill_ids(state: _State) -> set[str]:
    voided: set[str] = set()
    for effect in state.effects.values():
        if effect.kind == "fill_void" and effect.fill_id is not None:
            voided.add(effect.fill_id)
    return voided


def _cash_add(state: _State, delta: Decimal, message: str) -> None:
    updated = quantize_money(state.cash + delta)
    if updated < 0:
        raise NegativeCashRejected(message)
    state.cash = updated


def _require_currency_match(state: _State, instrument_id: str, currency: str) -> None:
    position = state.positions.get(instrument_id)
    if position is not None and position.currency != currency:
        raise InvalidEvent("instrument currency does not match the open position")
    for pending in state.pending.values():
        if pending.instrument_id == instrument_id and pending.instrument_currency != currency:
            raise InvalidEvent("instrument currency does not match a pending fill")


def _assert_invariants(state: _State) -> None:
    if state.cash < 0 or state.cash != quantize_money(state.cash):
        raise LedgerRejected("cash invariant broken")
    if state.realised != quantize_money(state.realised):
        raise LedgerRejected("realised P&L invariant broken")
    for name, amount in state.fees.items():
        if amount < 0 or amount != quantize_money(amount):
            raise LedgerRejected(f"fee invariant broken for {name}")
    for position in state.positions.values():
        if position.quantity < 0 or position.total_cost_gbp < 0:
            raise LedgerRejected("position invariant broken")
        if position.quantity == 0 and position.total_cost_gbp != 0:
            raise LedgerRejected("flat position still carries cost")
        marked = position.mark_price is not None
        mark_parts = (
            position.mark_fx_to_gbp is not None,
            position.mark_event_id is not None,
            position.mark_timestamp is not None,
            position.mark_source is not None,
        )
        if any(part != marked for part in mark_parts):
            raise LedgerRejected("mark fields are inconsistent")
    overlap = set(state.pending) & set(state.settled_fills)
    if overlap:
        raise LedgerRejected("a fill cannot be both pending and settled")

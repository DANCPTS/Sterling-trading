"""GBP ledger facade.

``post`` validates the whole proposed suffix before appending, so a rejection
leaves the log untouched. Baselines are events in that log. Loading replays
the file and does not capture a baseline for the wall-clock day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from sterling.errors import IdempotencyConflict, InvalidEvent, LedgerRejected, OutOfOrderTimestamp
from sterling.ledger.allocation import ClosedTrade
from sterling.ledger.calendar import london_date, require_aware
from sterling.ledger.engine import (
    Position,
    _State,
    equity_of,
    market_value,
    reduce_events,
)
from sterling.ledger.events import (
    EquitySnapshotEvent,
    FillVoidEvent,
    FxEvent,
    LedgerEvent,
    equity_snapshot,
)
from sterling.ledger.metrics import (
    DailyLossMetrics,
    consecutive_net_closed_losses,
    daily_loss_metrics,
    fee_allocation,
)
from sterling.ledger.money import quantize_money, require_non_negative_money
from sterling.ledger.reservations import Availability, summarise_availability
from sterling.ledger.store import read_ledger, write_ledger
from sterling.ledger.valuation import PositionValuation, accounting_valuation_status, open_valuations
from sterling.rules.model import DeskRules

_AVG_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class PositionView:
    instrument_id: str
    currency: str
    quantity: Decimal
    total_cost_gbp: Decimal
    average_cost_gbp: Decimal | None
    mark_price: Decimal | None
    mark_fx_to_gbp: Decimal | None
    market_value_gbp: Decimal
    unrealised_pnl_gbp: Decimal
    mark_timestamp: datetime | None
    mark_source: str | None
    valuation_status_for_accounting: str
    unallocated_entry_fees_gbp: Decimal


@dataclass(frozen=True, slots=True)
class LedgerView:
    cash_gbp: Decimal
    equity_gbp: Decimal
    realised_pnl_gbp: Decimal
    fees_gbp: tuple[tuple[str, Decimal], ...]
    positions: tuple[PositionView, ...]
    baselines: tuple[tuple[str, Decimal], ...]
    pending_fill_ids: tuple[str, ...]
    consecutive_net_closed_losses: int
    event_ids: tuple[str, ...]
    valuations: tuple[PositionValuation, ...]
    available_cash_gbp: Decimal | None
    available_cash_status: str
    gross_exposure_gbp: Decimal | None
    exposure_status: str


class Ledger:
    """Append-only simulated GBP ledger."""

    def __init__(
        self,
        initial_cash_gbp: Decimal | str,
        *,
        rules_version_id: str | None = None,
        rules_content_hash: str | None = None,
    ) -> None:
        self.initial_cash_gbp = require_non_negative_money(
            initial_cash_gbp,
            label="initial_cash_gbp",
        )
        self.rules_version_id = rules_version_id
        self.rules_content_hash = rules_content_hash
        self._events: list[LedgerEvent] = []

    @classmethod
    def from_events(
        cls,
        initial_cash_gbp: Decimal | str,
        events: tuple[LedgerEvent, ...] | list[LedgerEvent],
        *,
        rules_version_id: str | None = None,
        rules_content_hash: str | None = None,
    ) -> Ledger:
        initial = require_non_negative_money(initial_cash_gbp, label="initial_cash_gbp")
        materialised = list(events)
        reduce_events(initial, materialised)
        ledger = cls(
            initial,
            rules_version_id=rules_version_id,
            rules_content_hash=rules_content_hash,
        )
        ledger._events = materialised
        return ledger

    @classmethod
    def open_primary_account(cls, rules: DeskRules) -> Ledger:
        return cls(
            rules.starting_capital,
            rules_version_id=rules.version_id,
            rules_content_hash=rules.content_hash,
        )

    @classmethod
    def load(cls, path: Path) -> Ledger:
        loaded = read_ledger(path)
        return cls.from_events(
            loaded.initial_cash_gbp,
            loaded.events,
            rules_version_id=loaded.rules_version_id,
            rules_content_hash=loaded.rules_content_hash,
        )

    def save(self, path: Path) -> None:
        write_ledger(
            path,
            initial_cash_gbp=self.initial_cash_gbp,
            events=self._events,
            rules_version_id=self.rules_version_id,
            rules_content_hash=self.rules_content_hash,
        )

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    def post(self, event: LedgerEvent) -> None:
        """Append an event, or ignore an exact duplicate.

        A rejected FX settlement is not appended. If its fill is still pending,
        that fill is voided in the same step so it cannot settle later.
        """
        existing = self._find(event.event_id)
        if existing is not None:
            if existing != event:
                raise IdempotencyConflict(f"event id {event.event_id} was reused with a different payload")
            return
        if not isinstance(event, (EquitySnapshotEvent, FillVoidEvent)) and event.event_id.startswith(("day-baseline-", "void-")):
            raise InvalidEvent("event id prefix is reserved")
        proposed = self._with_baseline(event)
        for extra in proposed:
            if self._find(extra.event_id) is not None:
                raise IdempotencyConflict(f"event id {extra.event_id} already exists")
        try:
            reduce_events(self.initial_cash_gbp, [*self._events, *proposed])
        except LedgerRejected as exc:
            self._void_failed_settlement(event, exc)
            raise
        self._events.extend(proposed)

    def capture_day_baseline(self, as_of: datetime) -> None:
        """Persist the first validated equity snapshot of the London day of ``as_of``.

        A later call on the same London day does nothing. This does not use the
        wall clock unless the caller passes it.
        """
        aware = require_aware(as_of)
        day = london_date(aware)
        if day in self._baseline_days():
            return
        self.post(equity_snapshot(day, aware))

    def view(self, as_of: datetime | None = None) -> LedgerView:
        """Account view.

        With ``as_of``, balances are the log up to that instant. Without it,
        balances are current and no mark is labelled ACCOUNTING_SAME_DAY.
        """
        if as_of is None:
            return _view(self._state(), self._events, None)
        aware = require_aware(as_of)
        events = [event for event in self._events if event.timestamp <= aware]
        return _view(reduce_events(self.initial_cash_gbp, events), events, aware)

    def closed_trades(self, as_of: datetime | None = None) -> tuple[ClosedTrade, ...]:
        """Settled sells with entry fees allocated and exit fees included."""
        if as_of is None:
            events: list[LedgerEvent] = self._events
        else:
            aware = require_aware(as_of)
            events = [event for event in self._events if event.timestamp <= aware]
        state = reduce_events(self.initial_cash_gbp, events)
        return fee_allocation(state, events).trades

    def daily_metrics(self, as_of: datetime, rules: DeskRules) -> DailyLossMetrics:
        aware = require_aware(as_of)
        prefix = [event for event in self._events if event.timestamp <= aware]
        state = reduce_events(self.initial_cash_gbp, prefix)
        return daily_loss_metrics(state, prefix, aware, rules)

    def consecutive_net_closed_losses(self, as_of: datetime | None = None) -> int:
        if as_of is None:
            events = self._events
        else:
            aware = require_aware(as_of)
            events = [event for event in self._events if event.timestamp <= aware]
        state = reduce_events(self.initial_cash_gbp, events)
        return consecutive_net_closed_losses(state, events)

    def _void_failed_settlement(self, event: LedgerEvent, exc: LedgerRejected) -> None:
        if isinstance(exc, (OutOfOrderTimestamp, IdempotencyConflict)) or not isinstance(event, FxEvent):
            return
        if event.fill_id not in reduce_events(self.initial_cash_gbp, self._events).pending:
            return
        void = FillVoidEvent(f"void-{event.fill_id}", event.timestamp, event.fill_id, "settlement rejected")
        proposed = self._with_baseline(void)
        reduce_events(self.initial_cash_gbp, [*self._events, *proposed])
        self._events.extend(proposed)

    def _state(self) -> _State:
        return reduce_events(self.initial_cash_gbp, self._events)

    def _find(self, event_id: str) -> LedgerEvent | None:
        for event in self._events:
            if event.event_id == event_id:
                return event
        return None

    def _baseline_days(self) -> set[date]:
        return {london_date(event.timestamp) for event in self._events if isinstance(event, EquitySnapshotEvent)}

    def _with_baseline(self, event: LedgerEvent) -> list[LedgerEvent]:
        if isinstance(event, EquitySnapshotEvent):
            return [event]
        day = london_date(event.timestamp)
        if day in self._baseline_days():
            return [event]
        return [equity_snapshot(day, event.timestamp), event]


def _view(state: _State, events: list[LedgerEvent], as_of: datetime | None) -> LedgerView:
    allocation = fee_allocation(state, events)
    positions: list[PositionView] = []
    for instrument_id in sorted(state.positions):
        position = state.positions[instrument_id]
        if position.quantity == 0:
            continue
        unallocated = allocation.unallocated_entry_fees_gbp.get(instrument_id, Decimal("0.00"))
        positions.append(_position_view(position, as_of, unallocated))
    baselines = tuple((day.isoformat(), baseline.equity_gbp) for day, baseline in sorted(state.baselines.items()))
    fees = tuple(sorted(state.fees.items()))
    valuations = open_valuations(state, as_of)
    funds = _availability(state)
    return LedgerView(
        cash_gbp=state.cash,
        equity_gbp=equity_of(state),
        realised_pnl_gbp=state.realised,
        fees_gbp=fees,
        positions=tuple(positions),
        baselines=baselines,
        pending_fill_ids=tuple(sorted(state.pending)),
        consecutive_net_closed_losses=allocation.consecutive_net_closed_losses,
        event_ids=tuple(event.event_id for event in events),
        valuations=valuations,
        available_cash_gbp=funds.available_cash_gbp,
        available_cash_status=funds.available_cash_status,
        gross_exposure_gbp=funds.gross_exposure_gbp,
        exposure_status=funds.exposure_status,
    )


def _availability(state: _State) -> Availability:
    settled = Decimal("0.00")
    for position in state.positions.values():
        settled = quantize_money(settled + market_value(position))
    pending_buys = [fill for fill in state.pending.values() if fill.side == "buy"]
    return summarise_availability(
        cash_gbp=state.cash,
        pending_buys=pending_buys,
        settled_market_value_gbp=settled,
    )


def _position_view(position: Position, as_of: datetime | None, unallocated_entry_fees: Decimal) -> PositionView:
    value = market_value(position)
    average = (position.total_cost_gbp / position.quantity).quantize(
        _AVG_QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )
    return PositionView(
        instrument_id=position.instrument_id,
        currency=position.currency,
        quantity=position.quantity,
        total_cost_gbp=position.total_cost_gbp,
        average_cost_gbp=average,
        mark_price=position.mark_price,
        mark_fx_to_gbp=position.mark_fx_to_gbp,
        market_value_gbp=value,
        unrealised_pnl_gbp=quantize_money(value - position.total_cost_gbp),
        mark_timestamp=position.mark_timestamp,
        mark_source=position.mark_source,
        valuation_status_for_accounting=accounting_valuation_status(position, as_of),
        unallocated_entry_fees_gbp=unallocated_entry_fees,
    )

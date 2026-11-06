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

from sterling.errors import IdempotencyConflict, InvalidEvent
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
    LedgerEvent,
    equity_snapshot,
)
from sterling.ledger.metrics import (
    DailyLossMetrics,
    consecutive_net_closed_losses,
    daily_loss_metrics,
)
from sterling.ledger.money import quantize_money, require_non_negative_money
from sterling.ledger.store import read_ledger, write_ledger
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
        """Append an event, or ignore an exact duplicate. Rejected events do not append."""
        existing = self._find(event.event_id)
        if existing is not None:
            if existing != event:
                raise IdempotencyConflict(f"event id {event.event_id} was reused with a different payload")
            return
        if not isinstance(event, EquitySnapshotEvent) and event.event_id.startswith("day-baseline-"):
            raise InvalidEvent("event id prefix day-baseline- is reserved for day baselines")
        proposed = self._with_baseline(event)
        for extra in proposed:
            if self._find(extra.event_id) is not None:
                raise IdempotencyConflict(f"event id {extra.event_id} already exists")
        reduce_events(self.initial_cash_gbp, [*self._events, *proposed])
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

    def view(self) -> LedgerView:
        state = self._state()
        return _view(state, self._events)

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


def _view(state: _State, events: list[LedgerEvent]) -> LedgerView:
    positions: list[PositionView] = []
    for instrument_id in sorted(state.positions):
        position = state.positions[instrument_id]
        if position.quantity == 0:
            continue
        positions.append(_position_view(position))
    baselines = tuple((day.isoformat(), baseline.equity_gbp) for day, baseline in sorted(state.baselines.items()))
    fees = tuple(sorted(state.fees.items()))
    return LedgerView(
        cash_gbp=state.cash,
        equity_gbp=equity_of(state),
        realised_pnl_gbp=state.realised,
        fees_gbp=fees,
        positions=tuple(positions),
        baselines=baselines,
        pending_fill_ids=tuple(sorted(state.pending)),
        consecutive_net_closed_losses=consecutive_net_closed_losses(state, events),
        event_ids=tuple(event.event_id for event in events),
    )


def _position_view(position: Position) -> PositionView:
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
    )

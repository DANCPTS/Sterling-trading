"""Independent account view from an anchored Stage 1 ledger.

The Stage 1 file is replayed and hashed. It is not written here, and it is
not committed atomically with the risk journal. Episode nets use Stage 1
fee allocation, grouped flat-to-flat. Partial sells are not separate episodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sterling.gate2.canonical import sha256_bytes, sha256_file
from sterling.gate2.errors import StoreBlocked
from sterling.ledger.allocation import allocate
from sterling.ledger.calendar import london_date
from sterling.ledger.engine import Position, equity_of, reduce_events
from sterling.ledger.events import DepositEvent, FillEvent, LedgerEvent, WithdrawalEvent
from sterling.ledger.money import quantize_money
from sterling.ledger.reservations import buy_reservation_gbp, summarise_availability
from sterling.ledger.store import read_ledger

_ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class FlatEpisode:
    episode_id: str
    instrument_id: str
    origin_fill_id: str
    close_event_id: str
    close_index: int
    net_gbp: Decimal
    component_count: int


@dataclass(frozen=True, slots=True)
class OpenInstrument:
    instrument_id: str
    currency: str
    quantity: Decimal
    total_cost_gbp: Decimal
    episode_id: str
    origin_fill_id: str


@dataclass(frozen=True, slots=True)
class AnchoredLedger:
    """One complete, hash-checked Stage 1 file. Not a risk-journal transaction."""

    path: Path
    file_sha256: str
    initial_cash_gbp: Decimal
    events: tuple[LedgerEvent, ...]
    event_ids: tuple[str, ...]
    event_ids_hash: str
    cash_gbp: Decimal
    equity_gbp: Decimal
    available_cash_gbp: Decimal | None
    available_cash_status: str
    gross_exposure_gbp: Decimal | None
    exposure_status: str
    positions: tuple[Position, ...]
    pending_buys: tuple[FillEvent, ...]
    pending_sells: tuple[FillEvent, ...]
    episodes: tuple[FlatEpisode, ...]
    open_instruments: tuple[OpenInstrument, ...]
    stage1_baseline_dates: tuple[str, ...]
    jsonl_is_atomic_with_risk_journal: bool = False


def load_anchored_ledger(path: Path) -> AnchoredLedger:
    """Replay a Stage 1 JSONL file. Digest failure raises; it does not repair."""
    ledger_path = Path(path)
    if not ledger_path.is_file():
        raise StoreBlocked("LEDGER_MISSING")
    try:
        loaded = read_ledger(ledger_path)
        state = reduce_events(loaded.initial_cash_gbp, loaded.events)
    except Exception as exc:
        raise StoreBlocked(f"LEDGER_REJECTED: {exc}") from exc
    positions = tuple(state.positions.values())
    pending = tuple(state.pending.values())
    buys = tuple(fill for fill in pending if fill.side == "buy")
    sells = tuple(fill for fill in pending if fill.side == "sell")
    settled = Decimal("0.00")
    from sterling.ledger.engine import market_value

    for position in positions:
        settled = quantize_money(settled + market_value(position))
    funds = summarise_availability(
        cash_gbp=state.cash,
        pending_buys=buys,
        settled_market_value_gbp=settled,
    )
    event_ids = tuple(event.event_id for event in loaded.events)
    episodes, opens = _episodes(loaded.initial_cash_gbp, loaded.events)
    return AnchoredLedger(
        path=ledger_path,
        file_sha256=sha256_file(ledger_path),
        initial_cash_gbp=loaded.initial_cash_gbp,
        events=tuple(loaded.events),
        event_ids=event_ids,
        event_ids_hash=sha256_bytes("\n".join(event_ids).encode("utf-8")),
        cash_gbp=state.cash,
        equity_gbp=equity_of(state),
        available_cash_gbp=funds.available_cash_gbp,
        available_cash_status=funds.available_cash_status,
        gross_exposure_gbp=funds.gross_exposure_gbp,
        exposure_status=funds.exposure_status,
        positions=positions,
        pending_buys=buys,
        pending_sells=sells,
        episodes=episodes,
        open_instruments=opens,
        stage1_baseline_dates=tuple(day.isoformat() for day in sorted(state.baselines)),
    )


def net_external_since(anchor: AnchoredLedger, *, event_count: int, baseline_day: date) -> Decimal:
    """Deposits minus withdrawals after the opening anchor, on that London day.

    Reversed Stage 1 events are omitted. This matches the Stage 1 day-loss
    flow term and does not treat a withdrawal as a trading loss.
    """
    state = reduce_events(anchor.initial_cash_gbp, anchor.events)
    net = _ZERO
    for index, event in enumerate(anchor.events):
        if index < event_count:
            continue
        if event.event_id in state.reversed_ids:
            continue
        if london_date(event.timestamp) != baseline_day:
            continue
        if isinstance(event, DepositEvent):
            net += event.amount_gbp
        elif isinstance(event, WithdrawalEvent):
            net -= event.amount_gbp
    return quantize_money(net)


def prefix_matches(anchor: AnchoredLedger, event_ids_hash: str, event_count: int) -> bool:
    if event_count > len(anchor.event_ids):
        return False
    prefix = anchor.event_ids[:event_count]
    digest = sha256_bytes("\n".join(prefix).encode("utf-8"))
    return digest == event_ids_hash


def buy_cash_hold(fill: FillEvent) -> Decimal | None:
    """Stage 1 reservation. ``None`` means the GBP amount is unknown."""
    return buy_reservation_gbp(fill)


def _episodes(
    initial_cash: Decimal,
    events: tuple[LedgerEvent, ...] | list[LedgerEvent],
) -> tuple[tuple[FlatEpisode, ...], tuple[OpenInstrument, ...]]:
    state = reduce_events(initial_cash, events)
    allocation = allocate(state, events)
    nets = {trade.fill_id: trade.net_gbp for trade in allocation.trades}
    quantity: dict[str, Decimal] = {}
    origin: dict[str, str] = {}
    components: dict[str, list[str]] = {}
    closed: list[FlatEpisode] = []
    for index, event in enumerate(events):
        if event.event_id in state.reversed_ids:
            continue
        effect = state.effects.get(event.event_id)
        if effect is None or effect.kind != "fx" or effect.instrument_id is None or effect.quantity_delta == 0:
            continue
        instrument = effect.instrument_id
        before = quantity.get(instrument, Decimal("0"))
        after = before + effect.quantity_delta
        if before == 0 and after > 0 and effect.side == "buy" and effect.fill_id is not None:
            origin[instrument] = effect.fill_id
            components[instrument] = []
        if effect.side == "sell" and effect.fill_id is not None:
            components.setdefault(instrument, []).append(effect.fill_id)
        if before > 0 and after == 0:
            fills = components.get(instrument, [])
            total = _ZERO
            for fill_id in fills:
                total = quantize_money(total + nets.get(fill_id, _ZERO))
            origin_fill = origin.get(instrument, fills[0] if fills else event.event_id)
            closed.append(
                FlatEpisode(
                    episode_id=f"{instrument}:{origin_fill}",
                    instrument_id=instrument,
                    origin_fill_id=origin_fill,
                    close_event_id=event.event_id,
                    close_index=index,
                    net_gbp=total if total != 0 else _ZERO,
                    component_count=len(fills),
                )
            )
            origin.pop(instrument, None)
            components.pop(instrument, None)
            quantity.pop(instrument, None)
            continue
        if after < 0:
            raise StoreBlocked("LEDGER_QUANTITY_NEGATIVE")
        if after == 0:
            quantity.pop(instrument, None)
        else:
            quantity[instrument] = after
    opens: list[OpenInstrument] = []
    for instrument, position in state.positions.items():
        if position.quantity == 0:
            continue
        origin_fill = origin.get(instrument, f"open:{instrument}")
        opens.append(
            OpenInstrument(
                instrument_id=instrument,
                currency=position.currency,
                quantity=position.quantity,
                total_cost_gbp=position.total_cost_gbp,
                episode_id=f"{instrument}:{origin_fill}",
                origin_fill_id=origin_fill,
            )
        )
    closed.sort(key=lambda episode: (episode.close_index, episode.close_event_id))
    return tuple(closed), tuple(opens)


def london_day(moment: datetime) -> date:
    return london_date(moment)

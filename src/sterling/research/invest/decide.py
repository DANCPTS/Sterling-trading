"""One paper-v2 decision. The provider is called at most once in a week."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.providers import ProviderEnvelope
from sterling.research.grok.schema import InstrumentQuote, MarketSnapshot, PaperPosition
from sterling.research.grok.timekeeping import week_key
from sterling.research.invest.book import Book, derived_actions, mark, weights_of
from sterling.research.invest.identity import EXECUTION_PERMISSION, INVEST_V2
from sterling.research.invest.schema import CASH, FUNDS, Allocation, AllocationFailure, parse_allocation
from sterling.research.money import ZERO, quantize_money


class _Provider(Protocol):
    def complete(self, snapshot: MarketSnapshot, *, prompt: str) -> ProviderEnvelope: ...


def run_invest_week(
    *,
    log_path: Path,
    book: Book,
    prices: dict[str, Decimal],
    tradable: set[str],
    as_of: datetime,
    provider: _Provider,
    prompt: str,
    before_call: Callable[[], None] | None = None,
    usage: dict[str, object] | None = None,
    mode: str = "simulated_clock",
) -> dict[str, object]:
    """Reserve the week, then accept one allocation or keep the current book."""
    log = _open(log_path, as_of)
    week = week_key(as_of)
    if _reserved(log, week):
        return {"provider_calls": 0, "status": "noop", "week_key": week}
    _append(
        log,
        as_of,
        "week_reserve",
        {"clock_source": _clock(mode), "mode": mode, "recorded_at": as_of.isoformat(), "week_key": week},
    )
    if before_call is not None:
        before_call()
    snapshot = _snapshot(book, prices, as_of)
    envelope = provider.complete(snapshot, prompt=prompt)
    parsed = parse_allocation(envelope.raw_response)
    current = weights_of(book, prices, as_of.date()) or {name: ZERO for name in (*FUNDS, CASH)}
    if isinstance(parsed, AllocationFailure):
        targets = current
        actions = {name: "HOLD" for name in FUNDS}
        gate = "hold"
        reason = parsed.reason
        confidence = ZERO
        raw = envelope.raw_response.replace("\x00", "")[:20000]
    else:
        targets = parsed.weights
        actions = derived_actions(current, targets, tradable)
        gate = "accepted"
        reason = "accepted"
        confidence = parsed.confidence
        raw = ""
    fields: dict[str, object] = {
        "accepted_weights": {name: format(targets[name], "f") for name in sorted(targets)},
        "cash_gbp": format(book.cash, "f"),
        "clock_source": _clock(mode),
        "confidence": format(confidence, "f"),
        "derived_actions": actions,
        "execution_permission": EXECUTION_PERMISSION,
        "experiment": INVEST_V2.experiment,
        "gate": gate,
        "mode": mode,
        "reasons": [reason],
        "recorded_at": as_of.isoformat(),
        "system": INVEST_V2.system,
        "week_key": week,
    }
    if gate != "accepted":
        fields["disposition"] = "HOLD"
        fields["raw_reply"] = raw
    moment = as_of
    if usage:
        moment = _append(log, moment, "model_usage", usage)
    moment = _append(log, moment, "decision", fields)
    book.pending = parsed.weights if gate == "accepted" and isinstance(parsed, Allocation) else None
    marked = mark(book, prices, as_of.date())
    nav = marked[0] if marked is not None else book.cash
    _append(
        log,
        moment,
        "state_commit",
        {"clock_source": _clock(mode), "mode": mode, "state_hash": _state_hash(book), "week_key": week},
    )
    return {
        "execution_permission": EXECUTION_PERMISSION,
        "gate": gate,
        "nav_gbp": format(nav, "f"),
        "provider_calls": 1,
        "status": "decided",
        "week_key": week,
    }


def _snapshot(book: Book, prices: dict[str, Decimal], as_of: datetime) -> MarketSnapshot:
    quotes: list[InstrumentQuote] = []
    positions: list[PaperPosition] = []
    for ticker in FUNDS:
        price = prices.get(ticker)
        if price is None or price <= 0:
            continue
        quotes.append(InstrumentQuote(ticker, price, as_of, "paper-v2"))
        quantity = book.quantity(ticker)
        if quantity > 0:
            positions.append(PaperPosition(ticker, quantity, quantize_money(quantity * price)))
    if not quotes:
        quotes.append(InstrumentQuote(FUNDS[0], Decimal("1"), as_of, "paper-v2"))
    nav = quantize_money(book.cash + sum((item.market_value_gbp for item in positions), ZERO))
    if nav <= 0:
        nav = Decimal("0.01")
    return MarketSnapshot(as_of=as_of, quotes=tuple(quotes), positions=tuple(positions), nav_gbp=nav, universe=FUNDS)


def _state_hash(book: Book) -> str:
    text = f"{book.cash}|{book.trades}|{sorted(book.quantities.items())}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reserved(log: ProposalLog, week: str) -> bool:
    return any(
        record.get("week_key") == week and record.get("record_type") in {"week_reserve", "decision"} for record in log.records()
    )


def _open(path: Path, as_of: datetime) -> ProposalLog:
    log = ProposalLog(path)
    if not path.exists():
        log.create(now=as_of - timedelta(seconds=1))
    return log


def _append(log: ProposalLog, when: datetime, record_type: str, fields: dict[str, object]) -> datetime:
    previous = datetime.fromisoformat(str(log.records()[-1]["timestamp_utc"]))
    stamped = when if when > previous else previous + timedelta(microseconds=1)
    log.append_event(timestamp_utc=stamped, record_type=record_type, fields=fields)
    return stamped


def _clock(mode: str) -> str:
    return "system" if mode == "forward" else "injected"

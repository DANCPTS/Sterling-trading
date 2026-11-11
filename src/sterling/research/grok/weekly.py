"""One paper decision per ISO week, reconstructed from the log.

Forward mode reads the injected clock, which is the system clock unless a
test or a labelled simulated advance replaces it. A second call in the same
week does not call the provider and does not trade.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sterling.research.costs import quote_trade, research_cost_model
from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog, prompt_hash, snapshot_hash
from sterling.research.grok.context import PriceObservation
from sterling.research.grok.portfolio import PaperBook, replay, state_hash, write_state
from sterling.research.grok.safety import pinned_review
from sterling.research.grok.schema import (
    MarketSnapshot,
    ParseFailure,
    estimate_tokens,
    parse_response,
    prompt_for,
    response_hash,
)
from sterling.research.grok.timekeeping import Clock, SystemClock, require_mode, week_key
from sterling.research.grok.verify import verify_modes
from sterling.research.money import ZERO, quantize_money, quantize_qty_down

Provider = Callable[[MarketSnapshot, str], tuple[str, str]]


@dataclass(frozen=True, slots=True)
class WeekResult:
    status: str
    week_key: str
    mode: str
    state_hash: str
    provider_calls: int
    gate: str


def run_week(
    *,
    log_path: Path,
    state_path: Path,
    snapshot: MarketSnapshot,
    provider: Provider,
    mode: str,
    clock: Clock | None = None,
    recorder: Clock | None = None,
    opens: Mapping[str, tuple[str, str, str, str]] | None = None,
    closes: Mapping[str, tuple[str, str, int]] | None = None,
    prints: Sequence[PriceObservation] | None = None,
    hold_reasons: Mapping[str, str] | None = None,
    before_call: Callable[[], None] | None = None,
) -> WeekResult:
    """Settle the previous pending orders, then decide this week once."""
    del prints
    chosen = require_mode(mode)
    event_clock = clock or SystemClock()
    write_clock = recorder or SystemClock()
    log = _open(log_path, write_clock)
    book = replay(log.records())
    _check_existing(log, write_clock)
    decision_at = event_clock.now()
    week = week_key(decision_at)
    recorded_at = write_clock.now()
    _guard_times(chosen, decision_at, recorded_at)
    if week in book.reserved or week in book.weeks:
        _check_existing(log, write_clock)
        if not state_path.exists():
            write_state(state_path, book, log_sha=_tip(log))
        return WeekResult("noop", week, chosen, state_hash(book), 0, "noop")
    outcome = _settle(
        log,
        book,
        opens or {},
        closes or {},
        recorded_at,
        chosen,
        write_clock,
        decision_clock=event_clock,
    )
    if outcome == "carry":
        verify_modes(log.records(), now=write_clock.now())
        return WeekResult("carried", week, chosen, state_hash(replay(log.records())), 0, "carry")
    book = replay(log.records())
    _reserve(log, week, chosen, decision_at, recorded_at)
    if before_call is not None:
        before_call()
    request_at = event_clock.now()
    prompt = prompt_for(snapshot)
    forced: str | None = None
    try:
        raw, _model = provider(snapshot, prompt)
    except Exception as exc:
        if not _provider_responded(exc):
            raise
        raw = _sanitise(str(exc))
        forced = raw[:300] or "provider_error"
        _log_usage(log, exc, write_clock.now())
    response_at = event_clock.now()
    recorded_at = write_clock.now()
    if response_at < request_at:
        raise ResearchError("response time is before the request time")
    if chosen == "forward" and recorded_at < response_at:
        raise ResearchError("decision was recorded before the response")
    if forced is None:
        weights, gate, reasons = _screen(snapshot, prompt, raw, hold_reasons or {})
    else:
        weights, gate, reasons = {}, "hold", [forced]
    clock_source = "system" if chosen == "forward" else "injected"
    fields: dict[str, object] = {
        "accepted_weights": weights,
        "clock_source": clock_source,
        "decision_at": decision_at.isoformat(),
        "gate": gate,
        "reasons": reasons,
        "hold_reasons": {name: reason for name, reason in sorted((hold_reasons or {}).items())},
        "mode": chosen,
        "prompt_token_estimate": estimate_tokens(prompt_for(snapshot)),
        "recorded_at": recorded_at.isoformat(),
        "request_at": request_at.isoformat(),
        "response_at": response_at.isoformat(),
        "snapshot_hash": snapshot_hash({quote.instrument_id: ((quote.observed_at, quote.price),) for quote in snapshot.quotes}),
        "week_key": week,
    }
    if gate != "accepted":
        fields["disposition"] = "HOLD"
        fields["raw_reply"] = _sanitise(raw)
    log.append_event(
        timestamp_utc=_after(log, recorded_at),
        record_type="decision",
        fields=fields,
    )
    book = replay(log.records())
    digest = write_state(state_path, book, log_sha=_tip(log))
    log.append_event(
        timestamp_utc=_after(log, recorded_at),
        record_type="state_commit",
        fields={"mode": chosen, "state_hash": digest, "week_key": week},
    )
    verify_modes(log.records(), now=write_clock.now())
    return WeekResult("decided", week, chosen, digest, 1, gate)


def record_unquoted_week(
    *,
    log_path: Path,
    state_path: Path,
    mode: str,
    hold_reasons: Mapping[str, str],
    clock: Clock | None = None,
    recorder: Clock | None = None,
) -> WeekResult:
    """Record HOLD for a week whose prices are missing, stale, or not cross-checked."""
    chosen = require_mode(mode)
    event_clock = clock or SystemClock()
    write_clock = recorder or SystemClock()
    log = _open(log_path, write_clock)
    book = replay(log.records())
    _check_existing(log, write_clock)
    decision_at = event_clock.now()
    week = week_key(decision_at)
    recorded_at = write_clock.now()
    _guard_times(chosen, decision_at, recorded_at)
    if week in book.reserved or week in book.weeks:
        if not state_path.exists():
            write_state(state_path, book, log_sha=_tip(log))
        return WeekResult("noop", week, chosen, state_hash(book), 0, "noop")
    return _hold_without_call(log, week, chosen, decision_at, recorded_at, write_clock, state_path, hold_reasons)


def flag_missed_weeks(
    log_path: Path,
    expected: Sequence[str],
    *,
    mode: str,
    recorder: Clock | None = None,
) -> tuple[str, ...]:
    log = ProposalLog(log_path)
    write_clock = recorder or SystemClock()
    missing = []
    present = {
        value
        for record in log.records()
        if record.get("record_type") in {"decision", "missed_week", "week_reserve"}
        for value in [record.get("week_key")]
        if isinstance(value, str)
    }
    for week in expected:
        if week not in present:
            missing.append(week)
    moment = write_clock.now()
    for week in missing:
        moment = moment + timedelta(microseconds=1)
        log.append_event(
            timestamp_utc=_after(log, moment),
            record_type="missed_week",
            fields={
                "clock_source": "injected" if mode == "simulated_clock" else "system",
                "detected_at": write_clock.now().isoformat(),
                "mode": mode,
                "note": "MISSED week. Not a backfill and not a trade.",
                "order": "detection",
                "recorded_at": write_clock.now().isoformat(),
                "week_key": week,
            },
        )
    return tuple(missing)


def position_marks(
    held: Sequence[str],
    opens: Mapping[str, tuple[str, str, str, str]],
    closes: Mapping[str, tuple[str, str, int]],
    *,
    max_age: int = 5,
) -> dict[str, tuple[Decimal, str]] | None:
    """Mark each holding with its own open, or its own close. Never another name."""
    marks: dict[str, tuple[Decimal, str]] = {}
    for ticker in held:
        opened = opens.get(ticker)
        if opened is not None and str(opened[3]).endswith("-open"):
            marks[ticker] = (Decimal(opened[0]), "open")
            continue
        closed = closes.get(ticker)
        if closed is None or closed[2] > max_age or closed[2] < 0:
            return None
        marks[ticker] = (Decimal(closed[0]), "stale_close")
    return marks


def _settle(
    log: ProposalLog,
    book: PaperBook,
    opens: Mapping[str, tuple[str, str, str, str]],
    closes: Mapping[str, tuple[str, str, int]],
    recorded_at: datetime,
    mode: str,
    write_clock: Clock,
    *,
    decision_clock: Clock,
) -> str:
    if not book.pending_week or not book.pending_targets:
        return "ready"
    now = decision_clock.now()
    decision_at = datetime.fromisoformat(book.pending_decision_at) if book.pending_decision_at else now
    marks = position_marks(tuple(book.quantities), opens, closes)
    if marks is None:
        _skip(log, book, "valuation_invalid", recorded_at, mode, write_clock)
        return "carry"
    usable = {ticker: _usable_open(opens.get(ticker), decision_at, write_clock.now(), mode) for ticker in book.pending_targets}
    if any(item is None for item in usable.values()):
        if _sessions_after(decision_at, now) > 5:
            for ticker, opened in usable.items():
                if opened is None:
                    _cancel(log, book, ticker, "opening_price_unavailable", recorded_at, mode, write_clock)
            return "ready"
        _skip(log, book, "no_opening_price", recorded_at, mode, write_clock)
        return "carry"
    cash = book.cash
    nav = cash
    for name, quantity_held in book.quantities.items():
        nav += quantity_held * marks[name][0]
    for ticker in sorted(book.pending_targets):
        opened = usable[ticker]
        if opened is None:
            continue
        price, session = opened
        target = Decimal(book.pending_targets[ticker])
        goal = quantize_qty_down(target * nav / price) if price > 0 else ZERO
        held = book.quantities.get(ticker, ZERO)
        if goal == held:
            continue
        side = "BUY" if goal > held else "SELL"
        quantity = goal - held if side == "BUY" else held - goal
        model = research_cost_model(date.fromisoformat(session))
        quote = quote_trade(side, quantity, price, model, stamp=False, charge_fx=False)
        if quote.quantity == 0:
            continue
        cash = quantize_money(cash + quote.cash_delta_gbp)
        log.append_event(
            timestamp_utc=_after(log, recorded_at),
            record_type="fill",
            fields={
                "cash_after_gbp": format(cash, "f"),
                "cost_basis_gbp": format(quote.notional_gbp + quote.explicit_cost_gbp, "f"),
                "cost_gbp": format(quote.total_cost_gbp, "f"),
                "exec_price_gbp": format(quote.exec_price_gbp, "f"),
                "mode": mode,
                "open_gbp": format(price, "f"),
                "price_gbp": format(price, "f"),
                "price_role": "open",
                "quantity": format(quote.quantity, "f"),
                "recorded_at": write_clock.now().isoformat(),
                "session": session,
                "side": side,
                "source": opens[ticker][3],
                "spread_bps": format(model.spread_bps, "f"),
                "ticker": ticker,
                "week_key": book.pending_week,
            },
        )
    return "ready"


def _usable_open(
    opened: tuple[str, str, str, str] | None,
    decision_at: datetime,
    written_at: datetime,
    mode: str,
) -> tuple[Decimal, str] | None:
    """Accept an opening print only after the decision and not after it was written down."""
    if opened is None or not str(opened[3]).endswith("-open"):
        return None
    if mode == "forward" and opened[3] != "yahoo-open":
        return None
    available = datetime.fromisoformat(opened[2])
    if available <= decision_at or available > written_at:
        return None
    if available.date() != date.fromisoformat(opened[1]):
        return None
    return Decimal(opened[0]), opened[1]


def _sessions_after(decision_at: datetime, now: datetime) -> int:
    day = decision_at.date()
    count = 0
    while day < now.date():
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def _skip(log: ProposalLog, book: PaperBook, reason: str, recorded_at: datetime, mode: str, write_clock: Clock) -> None:
    log.append_event(
        timestamp_utc=_after(log, recorded_at),
        record_type="fill_skipped",
        fields={
            "mode": mode,
            "reason": reason,
            "recorded_at": write_clock.now().isoformat(),
            "tickers": ",".join(sorted(book.pending_targets)),
            "week_key": book.pending_week or "",
        },
    )


def _cancel(
    log: ProposalLog,
    book: PaperBook,
    ticker: str,
    reason: str,
    recorded_at: datetime,
    mode: str,
    write_clock: Clock,
) -> None:
    log.append_event(
        timestamp_utc=_after(log, recorded_at),
        record_type="order_cancelled",
        fields={
            "mode": mode,
            "reason": reason,
            "recorded_at": write_clock.now().isoformat(),
            "ticker": ticker,
            "week_key": book.pending_week or "",
        },
    )


def _hold_without_call(
    log: ProposalLog,
    week: str,
    mode: str,
    decision_at: datetime,
    recorded_at: datetime,
    write_clock: Clock,
    state_path: Path,
    hold_reasons: Mapping[str, str],
) -> WeekResult:
    """A week with no usable quote is a HOLD. It does not call the provider."""
    _reserve(log, week, mode, decision_at, recorded_at)
    reasons = {name: reason for name, reason in sorted(hold_reasons.items())} or {"universe": "no_usable_quote"}
    log.append_event(
        timestamp_utc=_after(log, recorded_at),
        record_type="decision",
        fields={
            "accepted_weights": {},
            "clock_source": "system" if mode == "forward" else "injected",
            "decision_at": decision_at.isoformat(),
            "gate": "hold",
            "reasons": ["no_usable_quote"],
            "hold_reasons": reasons,
            "mode": mode,
            "prompt_token_estimate": 0,
            "recorded_at": recorded_at.isoformat(),
            "request_at": decision_at.isoformat(),
            "response_at": decision_at.isoformat(),
            "snapshot_hash": "0" * 64,
            "week_key": week,
        },
    )
    book = replay(log.records())
    digest = write_state(state_path, book, log_sha=_tip(log))
    log.append_event(
        timestamp_utc=_after(log, recorded_at),
        record_type="state_commit",
        fields={"mode": mode, "state_hash": digest, "week_key": week},
    )
    verify_modes(log.records(), now=write_clock.now())
    return WeekResult("decided", week, mode, digest, 0, "hold")


def _provider_responded(exc: BaseException) -> bool:
    if getattr(exc, "usage", None) is not None:
        return True
    text = str(exc)
    return "after send" in text or "timed out" in text


def _sanitise(raw: str) -> str:
    text = raw.replace("\x00", "")
    key = os.environ.get("XAI_API_KEY", "")
    if key:
        text = text.replace(key, "[redacted]")
    if len(text) > 20000:
        text = text[:20000]
    return text


def _log_usage(log: ProposalLog, exc: BaseException, when: datetime) -> None:
    usage = getattr(exc, "usage", None)
    as_log = getattr(usage, "as_log", None)
    if not callable(as_log):
        return
    fields = as_log()
    if not isinstance(fields, dict):
        return
    log.append_event(timestamp_utc=_after(log, when), record_type="model_usage", fields=fields)


def _reserve(log: ProposalLog, week: str, mode: str, decision_at: datetime, recorded_at: datetime) -> None:
    log.append_event(
        timestamp_utc=_after(log, recorded_at),
        record_type="week_reserve",
        fields={
            "clock_source": "system" if mode == "forward" else "injected",
            "decision_at": decision_at.isoformat(),
            "mode": mode,
            "recorded_at": recorded_at.isoformat(),
            "request_at": decision_at.isoformat(),
            "response_at": decision_at.isoformat(),
            "week_key": week,
        },
    )


def _tip(log: ProposalLog) -> str:
    digest = log.records()[-1].get("record_hash")
    if not isinstance(digest, str):
        raise ResearchError("forward log hash is missing")
    return digest


def _open(path: Path, clock: Clock) -> ProposalLog:
    log = ProposalLog(path)
    if not path.exists():
        log.create(now=clock.now() - timedelta(seconds=1))
    return log


def _after(log: ProposalLog, moment: datetime) -> datetime:
    previous = datetime.fromisoformat(str(log.records()[-1]["timestamp_utc"]))
    candidate = moment if moment > previous else previous + timedelta(microseconds=1)
    return candidate


def _guard_times(mode: str, decision_at: datetime, recorded_at: datetime) -> None:
    if mode == "forward" and decision_at > recorded_at + timedelta(seconds=5):
        raise ResearchError("decision time is in the future")
    if mode in {"historical", "demo"} and decision_at.date() == recorded_at.date():
        raise ResearchError("historical decision is recorded as if it were made today")


def _check_existing(log: ProposalLog, clock: Clock) -> None:
    records = log.records()
    if any(record.get("mode") for record in records):
        verify_modes(records, now=clock.now())


def _screen(
    snapshot: MarketSnapshot,
    prompt: str,
    raw: str,
    hold_reasons: Mapping[str, str],
) -> tuple[dict[str, str], str, list[str]]:
    parsed = parse_response(raw, prompt_digest_value=prompt_hash(prompt), response_digest=response_hash(raw))
    if isinstance(parsed, ParseFailure):
        return {}, parsed.reason, [parsed.reason]
    instruments = []
    for item in parsed.instruments:
        if item.instrument_id in hold_reasons:
            continue
        row: dict[str, object] = {
            "action": item.action,
            "confidence": format(item.confidence, "f"),
            "instrument_id": item.instrument_id,
            "reasoning": item.reasoning,
        }
        if item.target_weight is not None:
            row["target_weight"] = format(item.target_weight, "f")
        instruments.append(row)
    if not instruments:
        return {}, "hold", ["hold"]
    review = pinned_review(
        {
            "account": {
                "marked_equity_gbp": "500.00",
                "net_external_deposits_gbp": "0.00",
                "opening_equity_gbp": "500.00",
            },
            "as_of": snapshot.as_of.isoformat(),
            "instruments": instruments,
            "paper_nav_gbp": format(snapshot.nav_gbp, "f"),
            "positions": [
                {
                    "instrument_id": position.instrument_id,
                    "market_value_gbp": format(position.market_value_gbp, "f"),
                    "quantity": format(position.quantity, "f"),
                }
                for position in snapshot.positions
            ],
            "quoted": [quote.instrument_id for quote in snapshot.quotes],
            "quotes": [{"instrument_id": quote.instrument_id, "price": format(quote.price, "f")} for quote in snapshot.quotes],
        }
    )
    raw_weights = review.get("weights")
    weights = {str(key): str(value) for key, value in raw_weights.items()} if isinstance(raw_weights, dict) else {}
    for ticker in hold_reasons:
        weights.pop(ticker, None)
    raw_reasons = review.get("reasons", [])
    reasons = [str(item) for item in raw_reasons] if isinstance(raw_reasons, list) else []
    gate = "accepted" if review.get("accepted_for_paper") is True and review.get("execution_permission") == "NOT_ISSUED" else "hold"
    if review.get("execution_permission") != "NOT_ISSUED":
        return {}, "screen_rejected", reasons or ["screen_rejected"]
    return weights, gate, reasons


def state_file(log_path: Path) -> Path:
    return log_path.with_suffix(".state.json")

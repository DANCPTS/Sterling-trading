"""One paper-v2 decision. The provider is called at most once in a week."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.providers import ProviderEnvelope
from sterling.research.grok.schema import InstrumentQuote, MarketSnapshot, PaperPosition, estimate_tokens
from sterling.research.grok.timekeeping import week_key
from sterling.research.invest.book import Book, _probe_nav, derived_actions, weights_of
from sterling.research.invest.identity import EXECUTION_PERMISSION, INVEST_V2
from sterling.research.invest.prompt import PROMPT_VERSION
from sterling.research.invest.registration import NO_TRADE_BAND
from sterling.research.invest.schema import CASH, FUNDS, Allocation, AllocationFailure, parse_allocation
from sterling.research.invest.sessions import first_session_after
from sterling.research.invest.snapshot import Print, build_invest_snapshot, prices_from_snapshot
from sterling.research.money import ZERO, quantize_money
from sterling.research.risk_config import load_research_limits
from sterling.research.risk_gate import fail_closed_allocation, paper_asset_classes


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
    prints: Sequence[Print] | None = None,
    sonia: Sequence[tuple[date, Decimal]] = (),
    fill_session: date | None = None,
) -> dict[str, object]:
    """Reserve the week, then accept one allocation or keep the current book.

    The research risk check runs after the reply is parsed and before
    ``book.pending`` changes. A breach is a HOLD. It is not resized.
    """
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
    research = build_invest_snapshot(
        book=book,
        prints=_prints(prints, prices, as_of),
        as_of=as_of,
        requested_tradable=tradable,
        sonia=sonia,
    )
    point_prices = prices_from_snapshot(research)
    market = _market_snapshot(book, point_prices, research, as_of)
    calls = 0
    envelope: ProviderEnvelope | None = None
    if market is None:
        parsed: Allocation | AllocationFailure = AllocationFailure("missing_valuation")
    else:
        envelope = provider.complete(market, prompt=prompt)
        calls = 1
        if usage is None:
            recorded = getattr(provider, "last_usage", None)
            if isinstance(recorded, dict):
                usage = recorded
        parsed = parse_allocation(envelope.raw_response, expected_model_id=envelope.model_id)
    current = weights_of(book, point_prices, as_of.date())
    if current is None:
        current = {name: ZERO for name in (*FUNDS, CASH)} if not _held_unpriced(book, point_prices) else None
    reply = "" if envelope is None else envelope.raw_response
    gate, reasons, targets, confidence, actions, raw = _disposition(
        parsed,
        current,
        book,
        point_prices,
        research,
        as_of,
        fill_session,
        reply,
    )
    if gate == "accepted" and isinstance(parsed, Allocation):
        from sterling.research.invest.daily import expire_replaced

        expire_replaced(log, book, as_of)
        book.pending = parsed.weights
    eligible = _eligible_session(as_of)
    fields: dict[str, object] = {
        "accepted_weights": {name: format(targets[name], "f") for name in sorted(targets)},
        "cash_gbp": format(book.cash, "f"),
        "clock_source": _clock(mode),
        "confidence": format(confidence, "f"),
        "derived_actions": actions,
        "eligible_session": "" if eligible is None else eligible.isoformat(),
        "execution": "next_session_open",
        "execution_permission": EXECUTION_PERMISSION,
        "experiment": INVEST_V2.experiment,
        "gate": gate,
        "mode": mode,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_text": prompt,
        "prompt_token_estimate": estimate_tokens(prompt),
        "prompt_version": PROMPT_VERSION,
        "reasons": reasons,
        "recorded_at": as_of.isoformat(),
        "research_snapshot": research,
        "risk_policy": "research-risk-v0",
        "risk_policy_operational": "false",
        "risk_policy_owner_approved": "false",
        "system": INVEST_V2.system,
        "week_key": week,
    }
    if isinstance(parsed, Allocation):
        fields["change_justification"] = parsed.change_justification
        fields["decision"] = parsed.decision
        fields["identified_risks"] = list(parsed.identified_risks)
        fields["portfolio_reasoning"] = parsed.portfolio_reasoning
        fields["verified_model_id"] = parsed.model_id
    if gate != "accepted":
        fields["disposition"] = "HOLD"
        fields["raw_reply"] = raw
    moment = as_of
    if usage:
        moment = _append(log, moment, "model_usage", usage)
    moment = _append(log, moment, "decision", fields)
    marked = _probe_nav(book, point_prices, as_of.date())
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
        "provider_calls": calls,
        "status": "decided",
        "week_key": week,
    }


def _disposition(
    parsed: Allocation | AllocationFailure,
    current: dict[str, Decimal] | None,
    book: Book,
    prices: dict[str, Decimal],
    research: dict[str, object],
    as_of: datetime,
    fill_session: date | None,
    reply: str,
) -> tuple[str, list[str], dict[str, Decimal], Decimal, dict[str, str], str]:
    held = {name: "HOLD" for name in FUNDS}
    flat = current or {name: ZERO for name in (*FUNDS, CASH)}
    stored = reply.replace("\x00", "")[:20000]
    if isinstance(parsed, AllocationFailure):
        return "hold", [parsed.reason], flat, ZERO, held, stored
    if current is None or _held_unpriced(book, prices):
        return "hold", ["missing_valuation"], flat, parsed.confidence, held, stored
    if parsed.decision in {"HOLD", "NO_TRADE"}:
        if _weights_differ(parsed.weights, current):
            return "hold", ["inconsistent_hold"], current, parsed.confidence, held, stored
        reason = "explicit_hold" if parsed.decision == "HOLD" else "explicit_no_trade"
        return "hold", [reason], current, parsed.confidence, held, stored
    from sterling.research.invest.snapshot import priced_names, stale_names, tradable_names

    limits = load_research_limits()
    marked = _probe_nav(book, prices, as_of.date())
    nav = None if marked is None else marked[0]
    decision = fail_closed_allocation(
        parsed.weights,
        current,
        limits,
        paper_asset_classes(),
        nav,
        tradable=tradable_names(research),
        stale=stale_names(research),
        priced=priced_names(research),
        eligible=set(FUNDS),
        decision_at=as_of,
        fill_session=_scheduled_fill(as_of, fill_session),
    )
    if not decision.accepted:
        return "hold", list(decision.reasons), current, parsed.confidence, held, stored
    active = tradable_names(research)
    return (
        "accepted",
        list(decision.reasons),
        parsed.weights,
        parsed.confidence,
        derived_actions(current, parsed.weights, active),
        "",
    )


def _weights_differ(proposed: dict[str, Decimal], current: dict[str, Decimal]) -> bool:
    for ticker in FUNDS:
        if abs(proposed.get(ticker, ZERO) - current.get(ticker, ZERO)) > NO_TRADE_BAND:
            return True
    return False


def _held_unpriced(book: Book, prices: dict[str, Decimal]) -> bool:
    return any(book.quantity(ticker) > 0 and ticker not in prices for ticker in FUNDS)


def _prints(prints: Sequence[Print] | None, prices: dict[str, Decimal], as_of: datetime) -> list[Print]:
    if prints is not None:
        return list(prints)
    return [(ticker, as_of.date(), price, as_of) for ticker, price in prices.items() if price > 0]


def _scheduled_fill(as_of: datetime, fill_session: date | None) -> date | None:
    if fill_session is not None:
        return fill_session
    return _eligible_session(as_of)


def _eligible_session(as_of: datetime) -> date | None:
    from sterling.research.errors import ResearchError

    try:
        return first_session_after(as_of)
    except ResearchError:
        return None


def _market_snapshot(
    book: Book,
    prices: dict[str, Decimal],
    research: dict[str, object],
    as_of: datetime,
) -> MarketSnapshot | None:
    from sterling.research.errors import ResearchError
    from sterling.research.fx.grok_forward import require_utc

    if as_of.tzinfo is None:
        return None
    try:
        stamped = require_utc(as_of.astimezone(UTC))
    except ResearchError:
        return None
    quotes: list[InstrumentQuote] = []
    positions: list[PaperPosition] = []
    for ticker in FUNDS:
        price = prices.get(ticker)
        if price is None or price <= 0:
            continue
        published = _published(research, ticker) or stamped
        if published > stamped:
            continue
        quotes.append(InstrumentQuote(ticker, price, published, "paper-v2"))
        quantity = book.quantity(ticker)
        if quantity > 0:
            positions.append(PaperPosition(ticker, quantity, quantize_money(quantity * price)))
    if not quotes:
        return None
    nav = quantize_money(book.cash + sum((item.market_value_gbp for item in positions), ZERO))
    if nav <= 0:
        return None
    return MarketSnapshot(as_of=stamped, quotes=tuple(quotes), positions=tuple(positions), nav_gbp=nav, universe=FUNDS)


def research_holdings(research: dict[str, object]) -> list[dict[str, object]]:
    portfolio = research.get("portfolio")
    if not isinstance(portfolio, dict):
        return []
    holdings = portfolio.get("holdings")
    if not isinstance(holdings, list):
        return []
    return [row for row in holdings if isinstance(row, dict)]


def _published(research: dict[str, object], ticker: str) -> datetime | None:
    for row in research_holdings(research):
        if row.get("instrument_id") != ticker or not isinstance(row.get("published_at"), str):
            continue
        try:
            return datetime.fromisoformat(str(row["published_at"]))
        except ValueError:
            return None
    return None


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

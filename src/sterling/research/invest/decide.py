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
from sterling.research.invest.book import Book, derived_actions, valuation_of
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
    research_snapshot: dict[str, object] | None = None,
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
    research = research_snapshot if research_snapshot is not None else _build(book, prints, prices, tradable, as_of, sonia)
    point_prices = prices_from_snapshot(research)
    valued = valuation_of(book, point_prices)
    current = _current_weights(valued)
    nav = None if valued is None else valued["nav"]
    market = _market_snapshot(book, point_prices, research, as_of)
    if _blocks_paid_synthetic(provider, research):
        return _fail_provider(
            log,
            book,
            point_prices,
            as_of,
            mode,
            week,
            _SyntheticXaiRefusal("synthetic-fixture prices cannot be sent to xAI. No request was sent."),
            usage,
        )
    calls = 0
    envelope: ProviderEnvelope | None = None
    try:
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
    except Exception as exc:
        return _fail_provider(log, book, point_prices, as_of, mode, week, exc, usage)
    reply = "" if envelope is None else envelope.raw_response
    gate, reasons, targets, confidence, actions, raw = _disposition(
        parsed,
        current,
        point_prices,
        research,
        as_of,
        fill_session,
        reply,
        envelope,
        nav,
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
    fields.update(_model_fields(envelope, parsed))
    if isinstance(parsed, Allocation):
        fields["change_justification"] = parsed.change_justification
        fields["decision"] = parsed.decision
        fields["identified_risks"] = list(parsed.identified_risks)
        fields["portfolio_reasoning"] = parsed.portfolio_reasoning
    if isinstance(parsed, AllocationFailure) and parsed.reason != "missing_valuation":
        _append(log, as_of, "provider_failure", _failure_body(week, "invalid_response", parsed.reason, "possibly_sent_or_billed"))
    if "provider_model_mismatch" in reasons:
        _append(
            log,
            as_of,
            "provider_failure",
            _failure_body(week, "model_identity", "provider_reported_model_disagrees", "possibly_sent_or_billed"),
        )
    if envelope is not None:
        fields["raw_reply"] = envelope.raw_response
        fields["raw_response_sha256"] = hashlib.sha256(envelope.raw_response.encode("utf-8")).hexdigest()
    if gate != "accepted":
        fields["disposition"] = "HOLD"
        if envelope is None:
            fields["raw_reply"] = raw
    moment = as_of
    if usage:
        moment = _append(log, moment, "model_usage", usage)
    moment = _append(log, moment, "decision", fields)
    reported_nav, nav_status = _nav_report(book, point_prices)
    _append(
        log,
        moment,
        "state_commit",
        {"clock_source": _clock(mode), "mode": mode, "state_hash": _state_hash(book), "week_key": week},
    )
    return {
        "execution_permission": EXECUTION_PERMISSION,
        "gate": gate,
        "nav_gbp": reported_nav,
        "nav_status": nav_status,
        "provider_calls": calls,
        "status": "decided",
        "week_key": week,
    }


def _disposition(
    parsed: Allocation | AllocationFailure,
    current: dict[str, Decimal] | None,
    prices: dict[str, Decimal],
    research: dict[str, object],
    as_of: datetime,
    fill_session: date | None,
    reply: str,
    envelope: ProviderEnvelope | None,
    nav: Decimal | None,
) -> tuple[str, list[str], dict[str, Decimal], Decimal, dict[str, str], str]:
    del prices
    held = {name: "HOLD" for name in FUNDS}
    flat = current or {name: ZERO for name in (*FUNDS, CASH)}
    stored = reply.replace("\x00", "")[:20000]
    if isinstance(parsed, AllocationFailure) or _reported_model_disagrees(envelope):
        reasons = [] if isinstance(parsed, Allocation) else [parsed.reason]
        if _reported_model_disagrees(envelope):
            reasons.append("provider_model_mismatch")
        confidence = ZERO if isinstance(parsed, AllocationFailure) else parsed.confidence
        return "hold", reasons, flat, confidence, held, stored
    if current is None:
        return "hold", ["missing_valuation"], flat, parsed.confidence, held, stored
    if parsed.decision in {"HOLD", "NO_TRADE"}:
        if _weights_differ(parsed.weights, current):
            return "hold", ["inconsistent_hold"], current, parsed.confidence, held, stored
        reason = "explicit_hold" if parsed.decision == "HOLD" else "explicit_no_trade"
        return "hold", [reason], current, parsed.confidence, held, stored
    from sterling.research.invest.snapshot import priced_names, stale_names, tradable_names

    limits = load_research_limits()
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


def _build(
    book: Book,
    prints: Sequence[Print] | None,
    prices: dict[str, Decimal],
    tradable: set[str],
    as_of: datetime,
    sonia: Sequence[tuple[date, Decimal]],
) -> dict[str, object]:
    if prints is None:
        synthetic = [(ticker, as_of.date(), price, as_of) for ticker, price in prices.items() if price > 0]
        return build_invest_snapshot(
            book=book,
            prints=synthetic,
            as_of=as_of,
            requested_tradable=tradable,
            sonia=sonia,
            provenance="synthetic-fixture",
        )
    return build_invest_snapshot(
        book=book,
        prints=prints,
        as_of=as_of,
        requested_tradable=tradable,
        sonia=sonia,
        provenance="supplied",
    )


def _current_weights(valued: dict[str, Decimal] | None) -> dict[str, Decimal] | None:
    if valued is None:
        return None
    return {name: valued[name] for name in (*FUNDS, CASH)}


def _reported_model_disagrees(envelope: ProviderEnvelope | None) -> bool:
    if envelope is None or envelope.provider_reported_model is None:
        return False
    return envelope.provider_reported_model != envelope.model_id


def _model_fields(envelope: ProviderEnvelope | None, parsed: Allocation | AllocationFailure) -> dict[str, object]:
    configured = "" if envelope is None else envelope.model_id
    reported = None if envelope is None else envelope.provider_reported_model
    if reported is None:
        identity = "absent"
    elif reported == configured:
        identity = "provider_reported"
    else:
        identity = "mismatch"
    fields: dict[str, object] = {
        "configured_model_id": configured,
        "provider_model_identity": identity,
        "provider_reported_model": "" if reported is None else reported,
    }
    if isinstance(parsed, Allocation):
        fields["json_model_id"] = parsed.model_id
    return fields


def _failure_body(week: str, kind: str, message: str, sent_state: str) -> dict[str, object]:
    return {
        "execution_permission": EXECUTION_PERMISSION,
        "kind": kind,
        "message": message[:500],
        "sent_state": sent_state,
        "week_key": week,
    }


class _SyntheticXaiRefusal(Exception):
    """A paid call was stopped before the client ran. Nothing was sent."""

    sent_state = "definitely_not_sent"


def _blocks_paid_synthetic(provider: object, research: dict[str, object]) -> bool:
    """Stop the real xAI client before it can send fixture prices."""
    if type(provider).__name__ != "LiveAllocationAdapter":
        return False
    client = getattr(provider, "_client", None)
    if type(client).__name__ != "XaiClient":
        return False
    availability = research.get("data_availability")
    if not isinstance(availability, dict):
        return True
    return str(availability.get("publication") or "") == "synthetic"


def _nav_report(book: Book, prices: dict[str, Decimal]) -> tuple[str | None, str]:
    """Authoritative NAV, or an explicit gap when a holding cannot be valued."""
    valued = valuation_of(book, prices)
    if valued is None:
        return None, "unavailable"
    return format(valued["nav"], "f"), "valued"


def _fail_provider(
    log: ProposalLog,
    book: Book,
    prices: dict[str, Decimal],
    as_of: datetime,
    mode: str,
    week: str,
    exc: Exception,
    usage: dict[str, object] | None,
) -> dict[str, object]:
    sent_state = str(getattr(exc, "sent_state", "possibly_sent_or_billed"))
    message = str(exc).replace("\n", " ")[:500]
    moment = as_of
    error_usage = getattr(exc, "usage", None)
    logged = usage
    if logged is None and error_usage is not None and hasattr(error_usage, "as_log"):
        recorded = error_usage.as_log()
        logged = recorded if isinstance(recorded, dict) else None
    if logged:
        moment = _append(log, moment, "model_usage", logged)
    nav_gbp, nav_status = _nav_report(book, prices)
    _append(
        log,
        moment,
        "provider_failure",
        {**_failure_body(week, type(exc).__name__, message, sent_state), "nav_gbp": nav_gbp, "nav_status": nav_status},
    )
    _append(
        log,
        moment,
        "state_commit",
        {"clock_source": _clock(mode), "mode": mode, "state_hash": _state_hash(book), "week_key": week},
    )
    calls = 0 if sent_state == "definitely_not_sent" else 1
    return {
        "execution_permission": EXECUTION_PERMISSION,
        "gate": "hold",
        "nav_gbp": nav_gbp,
        "nav_status": nav_status,
        "provider_calls": calls,
        "sent_state": sent_state,
        "status": "failed",
        "week_key": week,
    }


def _weights_differ(proposed: dict[str, Decimal], current: dict[str, Decimal]) -> bool:
    for ticker in FUNDS:
        if abs(proposed.get(ticker, ZERO) - current.get(ticker, ZERO)) > NO_TRADE_BAND:
            return True
    return False


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
    valued = valuation_of(book, prices)
    if valued is None or valued["nav"] <= 0:
        return None
    return MarketSnapshot(
        as_of=stamped,
        quotes=tuple(quotes),
        positions=tuple(positions),
        nav_gbp=valued["nav"],
        universe=FUNDS,
    )


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

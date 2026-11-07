"""Trusted generation ingress.

Only a recorded ingress principal may supply evidence. Proposal fields cannot
mark a generation trusted. The Stage 1 ledger is read and anchored; it is not
written and it is not atomic with this journal.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

from sterling.gate2.canonical import sha256_canonical
from sterling.gate2.errors import StoreBlocked, StoreUnavailable
from sterling.gate2.faults import fault_point
from sterling.gate2.models import DecisionResult, FxEvidence, GenerationInput, HaltName, HoldingQuote, Outcome, TrustedClock
from sterling.gate2.state import (
    active_halts,
    baseline_row,
    episode_history_hash,
    resolve_halt,
    streak_latch_required,
    trigger_halt,
)
from sterling.gate2.store import Gate2Store
from sterling.gate2.view import AnchoredLedger, load_anchored_ledger, london_day, net_external_since, prefix_matches
from sterling.ledger.calendar import london_date
from sterling.ledger.money import quantize_money
from sterling.risk.budget import daily_loss_threshold_cap, day_loss_gbp, planned_entry_risk_gbp
from sterling.risk.contracts import ClockInput, FixtureOperationalLimits, FxQuoteInput, QuoteInput, SessionInput
from sterling.risk.policy import Stage2Policy, load_default_policy
from sterling.risk.reasons import Reason
from sterling.risk.validation import _clock, _fx, _quote, _session

_ZERO = Decimal("0.00")


def ingest(
    store: Gate2Store,
    generation: GenerationInput,
    clock: TrustedClock,
    *,
    peer_uid: int,
    policy: Stage2Policy | None = None,
) -> DecisionResult:
    """Record one source generation. A conflicting duplicate is an integrity failure."""
    loaded_policy = policy if policy is not None else load_default_policy()
    if store.identity.policy_hash != loaded_policy.content_hash:
        return _blocked("POLICY_HASH_MISMATCH")
    if peer_uid != store.identity.ingress_uid:
        return _denied(("INGRESS_UID_REJECTED",), None)
    payload_hash = sha256_canonical(_generation_payload(generation))
    try:
        with store.immediate() as connection:
            fault_point("ingress_after_begin")
            result = _ingest_locked(store, connection, generation, clock, loaded_policy, payload_hash)
        return _finish(store, result)
    except StoreBlocked as exc:
        return _blocked(exc.reason)
    except (StoreUnavailable, OSError, sqlite3.OperationalError) as exc:
        return DecisionResult(
            outcome=Outcome.UNAVAILABLE,
            reasons=(str(exc),),
            reservation_id=None,
            reservation_state=None,
            replayed=False,
        )


def _ingest_locked(
    store: Gate2Store,
    connection: sqlite3.Connection,
    generation: GenerationInput,
    clock: TrustedClock,
    loaded_policy: Stage2Policy,
    payload_hash: str,
) -> DecisionResult:
    existing = connection.execute(
        """
        SELECT generation_id, payload_hash, admissible FROM generations
        WHERE source_id = ? AND source_sequence = ?
        """,
        (generation.source_id, generation.source_sequence),
    ).fetchone()
    if existing is not None:
        if str(existing["payload_hash"]) == payload_hash:
            fault_point("ingress_before_commit")
            reasons: tuple[str, ...] = ("GENERATION_REPLAYED",)
            if existing["admissible"]:
                reasons = ("GENERATION_REPLAYED", "GENERATION_ANCHORED")
            else:
                reasons = ("GENERATION_REPLAYED", "GENERATION_NOT_ADMISSIBLE")
            _outbox(store, connection, "generation-replay")
            return DecisionResult(
                outcome=Outcome.DENIED,
                reasons=reasons,
                reservation_id=None,
                reservation_state=None,
                replayed=True,
            )
        trigger_halt(
            store,
            connection,
            HaltName.INTEGRITY,
            {"conflict": "source_sequence", "source_id": generation.source_id},
        )
        _outbox(store, connection, "generation-conflict")
        return _denied(("GENERATION_CONFLICT",), None)
    last = connection.execute(
        "SELECT MAX(source_sequence) AS seq FROM generations WHERE source_id = ?",
        (generation.source_id,),
    ).fetchone()
    last_seq = 0 if last is None or last["seq"] is None else int(last["seq"])
    if generation.source_sequence != last_seq + 1:
        trigger_halt(
            store,
            connection,
            HaltName.INTEGRITY,
            {"conflict": "out_of_order", "expected": last_seq + 1, "got": generation.source_sequence},
        )
        _outbox(store, connection, "generation-order")
        return _denied(("GENERATION_OUT_OF_ORDER",), None)
    reasons_list, anchor, admissible = _assess(store, connection, generation, clock, loaded_policy)
    if anchor is None:
        trigger_halt(store, connection, HaltName.INTEGRITY, {"reasons": list(reasons_list)})
        _outbox(store, connection, "generation-unreadable")
        return _denied(reasons_list, None)
    generation_id = sha256_canonical({"payload": payload_hash, "ledger": anchor.file_sha256})[:32]
    _apply_anchor(
        store,
        connection,
        generation,
        clock,
        loaded_policy,
        anchor,
        generation_id,
        payload_hash,
        admissible,
        reasons_list,
    )
    fault_point("ingress_before_commit")
    _outbox(store, connection, generation_id)
    reported = reasons_list if not admissible else ("GENERATION_ANCHORED",)
    return DecisionResult(
        outcome=Outcome.DENIED,
        reasons=reported,
        reservation_id=None,
        reservation_state=None,
        replayed=False,
    )


def _finish(store: Gate2Store, result: DecisionResult) -> DecisionResult:
    try:
        store.publish_checkpoint()
    except StoreUnavailable as exc:
        return DecisionResult(
            outcome=Outcome.UNAVAILABLE,
            reasons=(*result.reasons, exc.reason),
            reservation_id=result.reservation_id,
            reservation_state=result.reservation_state,
            replayed=result.replayed,
            chain_seq=result.chain_seq,
        )
    return result


def _assess(
    store: Gate2Store,
    connection: sqlite3.Connection,
    generation: GenerationInput,
    clock: TrustedClock,
    policy: Stage2Policy,
) -> tuple[tuple[str, ...], AnchoredLedger | None, bool]:
    reasons: list[str] = []
    if generation.calendar_version != store.identity.calendar_version:
        reasons.append(Reason.CALENDAR_REJECTED.value)
    if store.identity.synthetic_fixture_scope:
        if generation.fixture_limits is None:
            reasons.append(Reason.SETTINGS_UNAPPROVED_UNCONFIGURED.value)
    elif generation.fixture_limits is not None:
        reasons.append(Reason.FIXTURE_NOT_OWNER_APPROVED.value)
        reasons.append("FIXTURE_SCOPE_NOT_ENABLED")
    else:
        reasons.append(Reason.SETTINGS_UNAPPROVED_UNCONFIGURED.value)
    if generation.opening_claim not in {"FIRST_FRESH_OPENING", "NONE"}:
        reasons.append(Reason.OPENING_BASELINE_REJECTED.value)
    from pathlib import Path

    try:
        anchor = load_anchored_ledger(Path(generation.ledger_path))
    except StoreBlocked as exc:
        return (exc.reason,), None, False
    _clock_reasons(connection, generation, clock, reasons)
    _compare_reported(generation, anchor, reasons)
    _quote_reasons(generation, clock, anchor, reasons)
    if not generation.costs_final:
        reasons.append("COSTS_NOT_FINAL")
    if generation.ambiguous_fill_ids:
        reasons.append("AMBIGUOUS_PENDING")
    if anchor.exposure_status != "KNOWN" or anchor.available_cash_status != "KNOWN":
        reasons.append(Reason.EXPOSURE_UNKNOWN.value)
    prior = connection.execute("SELECT event_ids_hash, event_count FROM baselines ORDER BY london_date LIMIT 1").fetchone()
    baselines = connection.execute("SELECT event_ids_hash, event_count, london_date FROM baselines").fetchall()
    for row in baselines:
        if not prefix_matches(anchor, str(row["event_ids_hash"]), int(row["event_count"])):
            reasons.append("LEDGER_PREFIX_MISMATCH")
            break
    del prior
    admissible = not reasons
    return tuple(reasons), anchor, admissible


def _apply_anchor(
    store: Gate2Store,
    connection: sqlite3.Connection,
    generation: GenerationInput,
    clock: TrustedClock,
    policy: Stage2Policy,
    anchor: AnchoredLedger,
    generation_id: str,
    payload_hash: str,
    admissible: bool,
    reasons: tuple[str, ...],
) -> None:
    day = london_date(generation.as_of)
    connection.execute(
        """
        INSERT INTO generations (
            generation_id, source_id, source_sequence, payload_hash, ledger_sha256,
            ledger_event_count, event_ids_hash, as_of, receipt_time, london_date,
            calendar_id, calendar_version, session_id, verified_trading_day,
            is_opening_snapshot, admissible, block_reasons, cash_gbp, equity_gbp,
            exposure_status, process_epoch, fixture_scope, fixture_json, opening_claim
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            generation_id,
            generation.source_id,
            generation.source_sequence,
            payload_hash,
            anchor.file_sha256,
            len(anchor.event_ids),
            anchor.event_ids_hash,
            generation.as_of.isoformat(),
            generation.receipt_time.isoformat(),
            day.isoformat(),
            generation.calendar_id,
            generation.calendar_version,
            generation.session_id,
            1 if generation.verified_trading_day else 0,
            1 if generation.is_opening_snapshot else 0,
            1 if admissible else 0,
            json.dumps(list(reasons)),
            format(anchor.cash_gbp, "f"),
            format(anchor.equity_gbp, "f"),
            anchor.exposure_status,
            store.process_epoch,
            1 if generation.fixture_limits is not None else 0,
            _fixture_json(generation.fixture_limits),
            generation.opening_claim,
        ),
    )
    store.append_event(
        connection,
        "GENERATION",
        {
            "admissible": admissible,
            "generation_id": generation_id,
            "ledger_path": generation.ledger_path,
            "ledger_sha256": anchor.file_sha256,
        },
    )
    from sterling.gate2.paths import remember_ledger_path

    remember_ledger_path(generation_id, generation.ledger_path)
    _write_quotes(connection, generation_id, generation)
    if admissible and generation.is_opening_snapshot and generation.opening_claim == "FIRST_FRESH_OPENING":
        if baseline_row(connection, day.isoformat()) is None and generation.verified_trading_day:
            seq = store.append_event(
                connection,
                "BASELINE",
                {"london_date": day.isoformat(), "equity_gbp": format(anchor.equity_gbp, "f"), "generation_id": generation_id},
            )
            connection.execute(
                """
                INSERT INTO baselines (
                    london_date, equity_gbp, generation_id, ledger_sha256, event_ids_hash,
                    event_count, external_flow_anchor, chain_seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day.isoformat(),
                    format(anchor.equity_gbp, "f"),
                    generation_id,
                    anchor.file_sha256,
                    anchor.event_ids_hash,
                    len(anchor.event_ids),
                    "0.00",
                    seq,
                ),
            )
    _replace_episodes(connection, anchor)
    _ratchet_floors(connection, generation, anchor)
    _link_pending(store, connection, generation, anchor, generation_id)
    _mark_ambiguous(store, connection, generation)
    if not admissible:
        data_reasons = [reason for reason in reasons if reason not in {"LEDGER_PREFIX_MISMATCH"}]
        if "LEDGER_PREFIX_MISMATCH" in reasons:
            trigger_halt(store, connection, HaltName.INTEGRITY, {"reasons": list(reasons)})
        if data_reasons:
            trigger_halt(store, connection, HaltName.DATA_UNTRUSTED, {"reasons": data_reasons, "generation_id": generation_id})
        connection.execute("UPDATE current_generation SET generation_id = ?, admissible = 0 WHERE id = 1", (generation_id,))
    else:
        if HaltName.DATA_UNTRUSTED.value in active_halts(connection):
            resolve_halt(
                store,
                connection,
                HaltName.DATA_UNTRUSTED,
                {"generation_id": generation_id},
                action="RESOLVE",
            )
        connection.execute("UPDATE current_generation SET generation_id = ?, admissible = 1 WHERE id = 1", (generation_id,))
        connection.execute("UPDATE meta SET last_clock = ? WHERE id = 1", (clock.as_of.isoformat(),))
        _latch_from_view(store, connection, generation, policy, anchor)
    must_streak, why = streak_latch_required(anchor.episodes, connection)
    if must_streak and generation.costs_final and admissible:
        trigger_halt(
            store,
            connection,
            HaltName.LOSS_STREAK,
            {"why": why, "history_hash": episode_history_hash(anchor.episodes), "generation_id": generation_id},
        )
    elif not generation.costs_final:
        trigger_halt(store, connection, HaltName.DATA_UNTRUSTED, {"costs_final": False})


def _latch_from_view(
    store: Gate2Store,
    connection: sqlite3.Connection,
    generation: GenerationInput,
    policy: Stage2Policy,
    anchor: AnchoredLedger,
) -> None:
    day = london_date(generation.as_of).isoformat()
    row = baseline_row(connection, day)
    if row is None:
        return
    opening = Decimal(str(row["equity_gbp"]))
    flows = net_external_since(anchor, event_count=int(row["event_count"]), baseline_day=london_date(generation.as_of))
    loss = day_loss_gbp(opening, anchor.equity_gbp, flows)
    threshold = daily_loss_threshold_cap(opening, policy)
    if loss >= threshold:
        trigger_halt(
            store,
            connection,
            HaltName.DAILY_LOSS,
            {
                "day_loss_gbp": format(loss, "f"),
                "threshold_gbp": format(threshold, "f"),
                "opening_gbp": format(opening, "f"),
                "equity_gbp": format(anchor.equity_gbp, "f"),
                "net_external_gbp": format(flows, "f"),
                "london_date": day,
                "generation_id": str(row["generation_id"]),
            },
        )


def _write_quotes(connection: sqlite3.Connection, generation_id: str, generation: GenerationInput) -> None:
    for instrument_id, quote in generation.quotes.items():
        stop = generation.holding_stops.get(instrument_id)
        connection.execute(
            """
            INSERT INTO holding_evidence (
                generation_id, instrument_id, mark_price, bid, ask, stop_price,
                covered_quantity, observed_at, source, quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                instrument_id,
                format(quote.mark, "f"),
                format(quote.bid, "f"),
                format(quote.ask, "f"),
                None if stop is None else format(stop.stop_price, "f"),
                None if stop is None else format(stop.covered_quantity, "f"),
                quote.observed_at.isoformat(),
                quote.source,
                quote.quality,
            ),
        )


def _replace_episodes(connection: sqlite3.Connection, anchor: AnchoredLedger) -> None:
    connection.execute("DELETE FROM episode_cache")
    for episode in anchor.episodes:
        connection.execute(
            """
            INSERT INTO episode_cache (
                episode_id, instrument_id, origin_fill_id, close_event_id, close_index, net_gbp, component_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode.episode_id,
                episode.instrument_id,
                episode.origin_fill_id,
                episode.close_event_id,
                episode.close_index,
                format(episode.net_gbp, "f"),
                episode.component_count,
            ),
        )


def _ratchet_floors(connection: sqlite3.Connection, generation: GenerationInput, anchor: AnchoredLedger) -> None:
    for instrument in anchor.open_instruments:
        stop = generation.holding_stops.get(instrument.instrument_id)
        if stop is None or instrument.quantity <= 0:
            continue
        if instrument.currency != "GBP":
            continue
        entry = (instrument.total_cost_gbp / instrument.quantity).quantize(Decimal("0.00000001"))
        try:
            floor = planned_entry_risk_gbp(
                entry_price=entry,
                stop_price=stop.stop_price,
                quantity=instrument.quantity,
                fx_rate_to_gbp=Decimal("1"),
                expected_entry_cost_gbp=_ZERO,
                expected_exit_cost_gbp=_ZERO,
                adverse_allowance_gbp=_ZERO,
            )
        except Exception:
            continue
        current = connection.execute(
            "SELECT floor_gbp FROM episode_risk WHERE episode_id = ?",
            (instrument.episode_id,),
        ).fetchone()
        if current is None:
            connection.execute(
                "INSERT INTO episode_risk (episode_id, instrument_id, floor_gbp) VALUES (?, ?, ?)",
                (instrument.episode_id, instrument.instrument_id, format(floor, "f")),
            )
        elif Decimal(str(current["floor_gbp"])) < floor:
            connection.execute(
                "UPDATE episode_risk SET floor_gbp = ? WHERE episode_id = ?",
                (format(floor, "f"), instrument.episode_id),
            )


def _link_pending(
    store: Gate2Store,
    connection: sqlite3.Connection,
    generation: GenerationInput,
    anchor: AnchoredLedger,
    generation_id: str,
) -> None:
    """Map each Stage 1 pending buy onto one risk row. Cash stays in the ledger figure."""
    from sterling.ledger.reservations import buy_reservation_gbp
    from sterling.risk.budget import conservative_notional_gbp, planned_entry_risk_gbp

    open_ids = {item.instrument_id: item.episode_id for item in anchor.open_instruments}
    for fill in anchor.pending_buys:
        existing = connection.execute(
            "SELECT reservation_id FROM reservations WHERE ledger_pending_fill_id = ?",
            (fill.event_id,),
        ).fetchone()
        if existing is not None:
            continue
        stop = generation.pending_stops.get(fill.event_id)
        cash = buy_reservation_gbp(fill)
        state = "RESERVED"
        if cash is None or stop is None or fill.instrument_currency != "GBP":
            state = "UNKNOWN"
            cash_text = "0.00"
            risk = _ZERO
            notional = _ZERO
            stop_price = fill.price
        else:
            cash_text = format(cash, "f")
            try:
                risk = planned_entry_risk_gbp(
                    entry_price=fill.price,
                    stop_price=stop.stop_price,
                    quantity=fill.quantity,
                    fx_rate_to_gbp=Decimal("1"),
                    expected_entry_cost_gbp=_ZERO,
                    expected_exit_cost_gbp=_ZERO,
                    adverse_allowance_gbp=_ZERO,
                )
            except Exception:
                state = "UNKNOWN"
                risk = _ZERO
            notional = conservative_notional_gbp(
                quantity=fill.quantity,
                price=fill.price,
                fx_rate_to_gbp=Decimal("1"),
            )
            stop_price = stop.stop_price
        episode_id = open_ids.get(fill.instrument_id, f"{fill.instrument_id}:{fill.event_id}")
        reservation_id = f"ledger-{fill.event_id}"
        seq = store.append_event(
            connection,
            "LEDGER_HOLD",
            {"fill_id": fill.event_id, "reservation_id": reservation_id, "state": state},
        )
        connection.execute(
            """
            INSERT INTO reservations (
                reservation_id, account_id, environment, request_id, principal, payload_hash,
                episode_id, instrument_id, quantity, entry_price, stop_price, conservative_price,
                cash_gbp, risk_gbp, notional_gbp, generation_id, policy_hash, state,
                cash_counted_in_ledger, ledger_pending_fill_id, executable, ticket_issued, created_seq
            ) VALUES (?, ?, 'simulated', ?, 'ledger-anchor', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, 0, ?)
            """,
            (
                reservation_id,
                store.identity.account_id,
                f"ledger:{fill.event_id}",
                fill.event_id,
                episode_id,
                fill.instrument_id,
                format(fill.quantity, "f"),
                format(fill.price, "f"),
                format(stop_price, "f"),
                format(fill.price, "f"),
                cash_text,
                format(risk, "f"),
                format(notional, "f"),
                generation_id,
                store.identity.policy_hash,
                state,
                fill.event_id,
                seq,
            ),
        )


def _mark_ambiguous(store: Gate2Store, connection: sqlite3.Connection, generation: GenerationInput) -> None:
    if not generation.ambiguous_fill_ids:
        return
    for fill_id in generation.ambiguous_fill_ids:
        rows = connection.execute(
            "SELECT reservation_id, state FROM reservations WHERE ledger_pending_fill_id = ?",
            (fill_id,),
        ).fetchall()
        for row in rows:
            if str(row["state"]) == "RELEASED":
                continue
            connection.execute(
                "UPDATE reservations SET state = 'UNKNOWN' WHERE reservation_id = ?",
                (str(row["reservation_id"]),),
            )
            seq = store.append_event(
                connection,
                "RESERVATION_UNKNOWN",
                {"reservation_id": str(row["reservation_id"]), "fill_id": fill_id},
            )
            connection.execute(
                """
                INSERT INTO reservation_events (reservation_id, chain_seq, state, evidence)
                VALUES (?, ?, 'UNKNOWN', ?)
                """,
                (str(row["reservation_id"]), seq, fill_id),
            )


def _clock_reasons(
    connection: sqlite3.Connection,
    generation: GenerationInput,
    clock: TrustedClock,
    reasons: list[str],
) -> None:
    findings: list[object] = []
    _clock(ClockInput(as_of=clock.as_of, previous_as_of=None), findings)  # type: ignore[arg-type]
    for finding in findings:
        reasons.append(str(finding.reason.value))  # type: ignore[attr-defined]
    row = connection.execute("SELECT last_clock FROM meta WHERE id = 1").fetchone()
    if row is not None and row["last_clock"] is not None:
        from datetime import datetime

        previous = datetime.fromisoformat(str(row["last_clock"]))
        if clock.as_of < previous:
            reasons.append(Reason.CLOCK_ROLLBACK.value)
    if generation.as_of > clock.as_of or generation.receipt_time > clock.as_of:
        reasons.append(Reason.QUOTE_TIMESTAMP_FUTURE.value)
    if london_day(generation.as_of) != london_date(clock.as_of):
        reasons.append(Reason.CLOCK_DATE_MISMATCH.value)


def _compare_reported(generation: GenerationInput, anchor: AnchoredLedger, reasons: list[str]) -> None:
    if generation.reported_cash_gbp is not None and quantize_money(generation.reported_cash_gbp) != anchor.cash_gbp:
        reasons.append("REPORTED_CASH_MISMATCH")
    if generation.reported_equity_gbp is not None and quantize_money(generation.reported_equity_gbp) != anchor.equity_gbp:
        reasons.append("REPORTED_EQUITY_MISMATCH")
    if (
        generation.reported_exposure_gbp is not None
        and anchor.gross_exposure_gbp is not None
        and quantize_money(generation.reported_exposure_gbp) != anchor.gross_exposure_gbp
    ):
        reasons.append("REPORTED_EXPOSURE_MISMATCH")


def _quote_reasons(
    generation: GenerationInput,
    clock: TrustedClock,
    anchor: AnchoredLedger,
    reasons: list[str],
) -> None:
    limits = generation.fixture_limits
    clock_input = ClockInput(as_of=clock.as_of)
    session = SessionInput(
        calendar_id=generation.calendar_id,
        session_id=generation.session_id,
        in_session=generation.in_session,
    )
    findings: list[object] = []
    _session(session, limits, findings)  # type: ignore[arg-type]
    seen: set[str] = set()
    for instrument in anchor.open_instruments:
        seen.add(instrument.instrument_id)
        quote = generation.quotes.get(instrument.instrument_id)
        if quote is None:
            reasons.append(f"MARK_MISSING:{instrument.instrument_id}")
            continue
        _one_quote(quote, clock_input, limits, findings)
        if instrument.currency != "GBP":
            fx = generation.fx.get(instrument.currency)
            if fx is None:
                reasons.append(f"FX_MISSING:{instrument.currency}")
            else:
                _one_fx(fx, clock_input, limits, findings)
        elif "GBP" in generation.fx:
            _one_fx(generation.fx["GBP"], clock_input, limits, findings)
        stop = generation.holding_stops.get(instrument.instrument_id)
        if stop is None or stop.covered_quantity < instrument.quantity:
            reasons.append(f"STOP_MISSING:{instrument.instrument_id}")
    for fill in anchor.pending_buys:
        if fill.instrument_id not in seen and fill.instrument_id not in generation.quotes:
            reasons.append(f"MARK_MISSING:{fill.instrument_id}")
        elif fill.instrument_id in generation.quotes:
            _one_quote(generation.quotes[fill.instrument_id], clock_input, limits, findings)
        if fill.event_id not in generation.pending_stops:
            reasons.append(f"STOP_MISSING:{fill.event_id}")
    for finding in findings:
        code = str(finding.reason.value)  # type: ignore[attr-defined]
        if code not in reasons:
            reasons.append(code)
    if limits is None:
        return
    max_age = timedelta(seconds=limits.quote_max_age_seconds)
    for quote in generation.quotes.values():
        if quote.quality == "ACCOUNTING_SAME_DAY" or quote.source == "ACCOUNTING_SAME_DAY":
            reasons.append("ACCOUNTING_LABEL_NOT_QUOTE")
        if quote.bid > quote.ask:
            reasons.append("QUOTE_CROSSED")
        if quote.mark < quote.bid or quote.mark > quote.ask:
            reasons.append("MARK_OUTSIDE_SPREAD")
        if clock.as_of - quote.observed_at > max_age:
            if Reason.QUOTE_STALE.value not in reasons:
                reasons.append(Reason.QUOTE_STALE.value)


def _one_quote(
    quote: HoldingQuote,
    clock: ClockInput,
    limits: FixtureOperationalLimits | None,
    findings: list[object],
) -> None:
    _quote(
        QuoteInput(
            bid=quote.bid,
            ask=quote.ask,
            observed_at=quote.observed_at,
            source=quote.source,
            quality=quote.quality,
        ),
        clock,
        limits,
        findings,  # type: ignore[arg-type]
    )


def _one_fx(
    fx: FxEvidence,
    clock: ClockInput,
    limits: FixtureOperationalLimits | None,
    findings: list[object],
) -> None:
    _fx(
        FxQuoteInput(rate_to_gbp=fx.rate_to_gbp, observed_at=fx.observed_at, source=fx.source),
        clock,
        limits,
        findings,  # type: ignore[arg-type]
    )


def _fixture_json(limits: FixtureOperationalLimits | None) -> str:
    if limits is None:
        return ""
    return json.dumps(
        {
            "calendar_id": limits.calendar_id,
            "fx_max_age_seconds": limits.fx_max_age_seconds,
            "fx_sources": sorted(limits.fx_sources),
            "label": limits.label,
            "owner_approved": limits.owner_approved,
            "quality_labels": sorted(limits.quality_labels),
            "quote_max_age_seconds": limits.quote_max_age_seconds,
            "quote_sources": sorted(limits.quote_sources),
            "session_id": limits.session_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _generation_payload(generation: GenerationInput) -> dict[str, object]:
    return {
        "ambiguous_fill_ids": list(generation.ambiguous_fill_ids),
        "calendar_id": generation.calendar_id,
        "calendar_version": generation.calendar_version,
        "costs_final": generation.costs_final,
        "fixture": _fixture_json(generation.fixture_limits),
        "in_session": generation.in_session,
        "is_opening_snapshot": generation.is_opening_snapshot,
        "ledger_path_name": generation.ledger_path,
        "opening_claim": generation.opening_claim,
        "reported_cash_gbp": None if generation.reported_cash_gbp is None else format(generation.reported_cash_gbp, "f"),
        "reported_equity_gbp": None if generation.reported_equity_gbp is None else format(generation.reported_equity_gbp, "f"),
        "reported_exposure_gbp": None
        if generation.reported_exposure_gbp is None
        else format(generation.reported_exposure_gbp, "f"),
        "session_id": generation.session_id,
        "source_id": generation.source_id,
        "source_sequence": generation.source_sequence,
        "verified_trading_day": generation.verified_trading_day,
    }


def _outbox(store: Gate2Store, connection: sqlite3.Connection, key: str) -> None:
    fault_point("before_outbox")
    seq = store.append_event(connection, "OUTBOX", {"key": key})
    connection.execute(
        "INSERT INTO audit_outbox (chain_seq, kind, payload_hash, published) VALUES (?, 'AUDIT', ?, 1)",
        (seq, key),
    )
    fault_point("after_outbox_write")


def _denied(reasons: tuple[str, ...], generation_id: str | None) -> DecisionResult:
    del generation_id
    return DecisionResult(
        outcome=Outcome.DENIED,
        reasons=reasons,
        reservation_id=None,
        reservation_state=None,
        replayed=False,
    )


def _blocked(reason: str) -> DecisionResult:
    return DecisionResult(
        outcome=Outcome.BLOCKED,
        reasons=(reason,),
        reservation_id=None,
        reservation_state=None,
        replayed=False,
    )

"""Atomic non-executable admission.

All budget dimensions move in one IMMEDIATE transaction or not at all.
The result never carries a ticket or an execution permission. Gate 1
Decimal functions perform the money arithmetic.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from sterling.gate2.canonical import sha256_canonical
from sterling.gate2.errors import StoreBlocked, StoreUnavailable
from sterling.gate2.faults import fault_point
from sterling.gate2.models import AdmissionRequest, DecisionResult, HaltName, Outcome, TrustedClock, proposal_payload
from sterling.gate2.paths import ledger_path, reload_ledger_paths
from sterling.gate2.state import active_halts, active_reservation_rows, baseline_row, trigger_halt
from sterling.gate2.store import Gate2Store
from sterling.gate2.view import load_anchored_ledger, net_external_since
from sterling.ledger.calendar import london_date
from sterling.ledger.money import quantize_money
from sterling.risk.budget import daily_loss_threshold_cap, day_loss_gbp, per_trade_risk_cap
from sterling.risk.contracts import (
    AccountInput,
    BaselineKind,
    BookPosition,
    ClockInput,
    ClosedEpisode,
    EntryProposal,
    FixtureOperationalLimits,
    Freshness,
    FxQuoteInput,
    KnownStatus,
    PositionStatus,
    QuoteInput,
    SessionInput,
    StopProtection,
)
from sterling.risk.policy import Stage2Policy, load_default_policy
from sterling.risk.reasons import Reason
from sterling.risk.validation import assess_long_entries

_ZERO = Decimal("0.00")
_ACTIVE = ("RESERVED", "CANCEL_PENDING", "UNKNOWN")


def admit(
    store: Gate2Store,
    request: AdmissionRequest,
    clock: TrustedClock,
    *,
    policy: Stage2Policy | None = None,
) -> DecisionResult:
    """Admit or deny one proposal. Replay of the same payload is not a second hold."""
    loaded = policy if policy is not None else load_default_policy()
    reload_ledger_paths(store)
    if store.identity.policy_hash != loaded.content_hash:
        return _result(Outcome.BLOCKED, ("POLICY_HASH_MISMATCH",))
    try:
        payload = proposal_payload(request)
    except Exception as exc:
        return _result(Outcome.REJECTED, (f"PROPOSAL_UNREADABLE:{exc}",))
    payload_hash = sha256_canonical(payload)
    try:
        with store.immediate() as connection:
            result = _admit_locked(store, connection, request, clock, loaded, payload_hash)
        return _publish(store, result)
    except StoreBlocked as exc:
        return _result(Outcome.BLOCKED, (exc.reason,))
    except (StoreUnavailable, OSError, sqlite3.OperationalError) as exc:
        return _result(Outcome.UNAVAILABLE, (str(exc),))


def lookup(
    store: Gate2Store,
    *,
    account_id: str,
    environment: str,
    principal: str,
    request_id: str,
) -> DecisionResult:
    """Read a committed decision. Works while the store is blocked so a lost reply can be resolved."""
    row = store.connection.execute(
        """
        SELECT * FROM decisions
        WHERE account_id = ? AND environment = ? AND principal = ? AND request_id = ?
        """,
        (account_id, environment, principal, request_id),
    ).fetchone()
    if row is None:
        return _result(Outcome.DENIED, ("DECISION_NOT_FOUND",))
    reservation_state = None
    if row["reservation_id"] is not None:
        held = store.connection.execute(
            "SELECT state FROM reservations WHERE reservation_id = ?",
            (str(row["reservation_id"]),),
        ).fetchone()
        if held is not None:
            reservation_state = str(held["state"])
    return DecisionResult(
        outcome=Outcome(str(row["outcome"])),
        reasons=tuple(json.loads(str(row["reasons"]))),
        reservation_id=None if row["reservation_id"] is None else str(row["reservation_id"]),
        reservation_state=reservation_state,
        replayed=True,
        chain_seq=int(row["chain_seq"]),
    )


def release_never_exported(
    store: Gate2Store,
    reservation_id: str,
    *,
    peer_uid: int,
    proof: str,
) -> DecisionResult:
    """Release a hold only with proof that no executor exists. TTL is not proof."""
    if proof == "TTL":
        return _result(Outcome.DENIED, ("TTL_IS_NOT_RELEASE_PROOF",))
    if proof != "NEVER_EXPORTED":
        return _result(Outcome.DENIED, ("RELEASE_PROOF_REJECTED",))
    if peer_uid != store.identity.risk_uid and peer_uid != store.identity.ingress_uid:
        return _result(Outcome.DENIED, ("RELEASE_UID_REJECTED",))
    executor = store.connection.execute("SELECT executor_present FROM meta WHERE id = 1").fetchone()
    if executor is not None and int(executor["executor_present"]) != 0:
        return _result(Outcome.DENIED, ("EXECUTOR_PRESENT",))
    try:
        with store.immediate() as connection:
            orders = connection.execute(
                "SELECT COUNT(*) AS n FROM audit_outbox WHERE kind NOT IN ('AUDIT', 'CHECKPOINT_NOTE')"
            ).fetchone()
            if orders is not None and int(orders["n"]) != 0:
                return _result(Outcome.DENIED, ("EXECUTOR_OUTBOX_NONEMPTY",))
            row = connection.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                return _result(Outcome.DENIED, ("RESERVATION_MISSING",))
            if str(row["state"]) == "RELEASED":
                result = _result(Outcome.DENIED, ("RELEASE_REPLAYED",), reservation_id, "RELEASED", replayed=True)
            elif str(row["state"]) == "UNKNOWN":
                result = _result(Outcome.DENIED, ("UNKNOWN_RETAINED",), reservation_id, "UNKNOWN")
            else:
                connection.execute(
                    "UPDATE reservations SET state = 'RELEASED' WHERE reservation_id = ?",
                    (reservation_id,),
                )
                seq = store.append_event(
                    connection,
                    "RESERVATION_RELEASED",
                    {"reservation_id": reservation_id, "proof": "NEVER_EXPORTED"},
                )
                connection.execute(
                    """
                    INSERT INTO reservation_events (reservation_id, chain_seq, state, evidence)
                    VALUES (?, ?, 'RELEASED', 'NEVER_EXPORTED')
                    """,
                    (reservation_id, seq),
                )
                connection.execute(
                    """
                    INSERT INTO audit (chain_seq, kind, actor_uid, reason, details, outcome)
                    VALUES (?, 'RELEASE', ?, 'never exported', ?, 'RELEASED')
                    """,
                    (seq, peer_uid, reservation_id),
                )
                result = _result(Outcome.DENIED, ("RELEASED_NON_EXECUTABLE",), reservation_id, "RELEASED")
        return _publish(store, result)
    except StoreBlocked as exc:
        return _result(Outcome.BLOCKED, (exc.reason,))
    except (StoreUnavailable, OSError, sqlite3.OperationalError) as exc:
        return _result(Outcome.UNAVAILABLE, (str(exc),))


def _admit_locked(
    store: Gate2Store,
    connection: sqlite3.Connection,
    request: AdmissionRequest,
    clock: TrustedClock,
    policy: Stage2Policy,
    payload_hash: str,
) -> DecisionResult:
    fault_point("after_begin")
    if request.account_id != store.identity.account_id or request.environment != "simulated":
        return _persist_denial(store, connection, request, payload_hash, ("ACCOUNT_OR_ENVIRONMENT_REJECTED",))
    existing = connection.execute(
        """
        SELECT * FROM decisions
        WHERE account_id = ? AND environment = ? AND principal = ? AND request_id = ?
        """,
        (request.account_id, request.environment, request.principal, request.request_id),
    ).fetchone()
    if existing is not None:
        if str(existing["payload_hash"]) != payload_hash:
            connection.execute(
                """
                INSERT INTO audit (chain_seq, kind, actor_uid, reason, details, outcome)
                VALUES (?, 'DUPLICATE_PAYLOAD', NULL, 'payload changed', ?, 'REJECTED')
                """,
                (
                    store.append_event(connection, "DUPLICATE_PAYLOAD", {"request_id": request.request_id}),
                    request.request_id,
                ),
            )
            return _result(Outcome.REJECTED, ("DUPLICATE_PAYLOAD",))
        state = None
        if existing["reservation_id"] is not None:
            held = connection.execute(
                "SELECT state FROM reservations WHERE reservation_id = ?",
                (str(existing["reservation_id"]),),
            ).fetchone()
            state = None if held is None else str(held["state"])
        return DecisionResult(
            outcome=Outcome(str(existing["outcome"])),
            reasons=tuple(json.loads(str(existing["reasons"]))),
            reservation_id=None if existing["reservation_id"] is None else str(existing["reservation_id"]),
            reservation_state=state,
            replayed=True,
            chain_seq=int(existing["chain_seq"]),
        )
    if request.authority_claimed():
        return _persist_denial(store, connection, request, payload_hash, ("FORGED_AUTHORITY",))
    current = connection.execute("SELECT generation_id, admissible FROM current_generation WHERE id = 1").fetchone()
    if current is None or current["generation_id"] is None or not current["admissible"]:
        return _persist_denial(store, connection, request, payload_hash, ("NO_ADMISSIBLE_GENERATION",))
    generation = connection.execute(
        "SELECT * FROM generations WHERE generation_id = ?",
        (str(current["generation_id"]),),
    ).fetchone()
    if generation is None:
        return _persist_denial(store, connection, request, payload_hash, ("GENERATION_MISSING",))
    try:
        anchor = load_anchored_ledger(Path(_ledger_path(store, generation)))
    except StoreBlocked as exc:
        trigger_halt(store, connection, HaltName.RECONCILIATION, {"reason": exc.reason})
        return _persist_denial(store, connection, request, payload_hash, ("LEDGER_UNREADABLE",))
    if anchor.file_sha256 != str(generation["ledger_sha256"]):
        trigger_halt(store, connection, HaltName.RECONCILIATION, {"reason": "ledger_hash_mismatch"})
        return _persist_denial(store, connection, request, payload_hash, ("LEDGER_HASH_MISMATCH",))
    _maybe_latch_daily(store, connection, clock, generation, anchor, policy)
    reasons = _pre_budget_reasons(store, connection, request, clock, generation, anchor, policy)
    if reasons:
        return _persist_denial(store, connection, request, payload_hash, tuple(reasons))
    assessment_reasons, figures_ok, proposal, episode_id, planned, notional, cash = _budgets(
        connection, request, clock, generation, anchor, policy
    )
    reasons.extend(assessment_reasons)
    if not figures_ok or planned is None or notional is None or cash is None or proposal is None or episode_id is None:
        return _persist_denial(store, connection, request, payload_hash, tuple(dict.fromkeys(reasons)))
    if reasons:
        return _persist_denial(store, connection, request, payload_hash, tuple(dict.fromkeys(reasons)))
    fault_point("after_reservation_write")
    reservation_id = uuid.uuid4().hex
    seq = store.append_event(
        connection,
        "RESERVATION",
        {
            "cash_gbp": format(cash, "f"),
            "episode_id": episode_id,
            "instrument_id": proposal.instrument_id,
            "notional_gbp": format(notional, "f"),
            "request_id": request.request_id,
            "reservation_id": reservation_id,
            "risk_gbp": format(planned, "f"),
        },
    )
    connection.execute(
        """
        INSERT INTO reservations (
            reservation_id, account_id, environment, request_id, principal, payload_hash,
            episode_id, instrument_id, quantity, entry_price, stop_price, conservative_price,
            cash_gbp, risk_gbp, notional_gbp, generation_id, policy_hash, state,
            cash_counted_in_ledger, ledger_pending_fill_id, executable, ticket_issued, created_seq
        ) VALUES (?, ?, 'simulated', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', 0, NULL, 0, 0, ?)
        """,
        (
            reservation_id,
            request.account_id,
            request.request_id,
            request.principal,
            payload_hash,
            episode_id,
            proposal.instrument_id,
            format(proposal.quantity, "f"),
            format(proposal.entry_price, "f"),
            format(proposal.stop_price if proposal.stop_price is not None else Decimal("0"), "f"),
            format(proposal.conservative_price, "f"),
            format(cash, "f"),
            format(planned, "f"),
            format(notional, "f"),
            str(generation["generation_id"]),
            policy.content_hash,
            seq,
        ),
    )
    connection.execute(
        """
        INSERT INTO reservation_events (reservation_id, chain_seq, state, evidence)
        VALUES (?, ?, 'RESERVED', 'non-executable')
        """,
        (reservation_id, seq),
    )
    fault_point("after_decision_write")
    _write_decision(
        connection,
        request,
        payload_hash,
        Outcome.RESERVED,
        ("NON_EXECUTABLE_HOLD",),
        reservation_id,
        seq,
    )
    fault_point("after_audit_write")
    connection.execute(
        """
        INSERT INTO audit (chain_seq, kind, actor_uid, reason, details, outcome)
        VALUES (?, 'ADMIT', NULL, 'non-executable hold', ?, 'RESERVED')
        """,
        (seq, reservation_id),
    )
    fault_point("before_commit")
    return DecisionResult(
        outcome=Outcome.RESERVED,
        reasons=("NON_EXECUTABLE_HOLD",),
        reservation_id=reservation_id,
        reservation_state="RESERVED",
        replayed=False,
        chain_seq=seq,
    )


def _budgets(
    connection: sqlite3.Connection,
    request: AdmissionRequest,
    clock: TrustedClock,
    generation: sqlite3.Row,
    anchor: object,
    policy: Stage2Policy,
) -> tuple[list[str], bool, EntryProposal | None, str | None, Decimal | None, Decimal | None, Decimal | None]:
    from sterling.gate2.view import AnchoredLedger

    assert isinstance(anchor, AnchoredLedger)
    reasons: list[str] = []
    holds = active_reservation_rows(connection)
    if any(str(row["state"]) == "UNKNOWN" for row in holds):
        reasons.append("UNKNOWN_HOLD_RETAINED")
    day = london_date(clock.as_of).isoformat()
    baseline = baseline_row(connection, day)
    if baseline is None:
        reasons.append(Reason.OPENING_BASELINE_REJECTED.value)
        return reasons, False, None, None, None, None, None
    opening = Decimal(str(baseline["equity_gbp"]))
    flows = net_external_since(anchor, event_count=int(baseline["event_count"]), baseline_day=london_date(clock.as_of))
    linked_ids = {str(row["ledger_pending_fill_id"]) for row in holds if row["ledger_pending_fill_id"] is not None}
    extra_cash = _ZERO
    for row in holds:
        if int(row["cash_counted_in_ledger"]) == 0:
            extra_cash = quantize_money(extra_cash + Decimal(str(row["cash_gbp"])))
    if anchor.available_cash_gbp is None or anchor.available_cash_status != "KNOWN":
        reasons.append(Reason.CASH_UNKNOWN.value)
        cash_available: Decimal | None = None
        cash_status = KnownStatus.UNKNOWN
    else:
        cash_available = quantize_money(anchor.available_cash_gbp - extra_cash)
        cash_status = KnownStatus.KNOWN
    positions = _book(anchor, holds, generation, connection)
    episode_id, opens_new = _bind_episode(request.proposal, anchor, holds)
    proposal = _rebound(request.proposal, episode_id, opens_new)
    closed = tuple(
        ClosedEpisode(
            episode_id=str(row["episode_id"]),
            origin_fill_id=str(row["origin_fill_id"]),
            net_gbp=Decimal(str(row["net_gbp"])),
            partial_exit=False,
        )
        for row in connection.execute("SELECT * FROM episode_cache ORDER BY close_index, close_event_id")
    )
    limits = _limits_from_json(str(generation["fixture_json"]))
    quote = _quote_for(connection, generation, proposal.instrument_id)
    fx_source = "FIXTURE-FX"
    if limits is not None and "FIXTURE-FX" not in limits.fx_sources and limits.fx_sources:
        fx_source = sorted(limits.fx_sources)[0]
    fx_quote = FxQuoteInput(rate_to_gbp=Decimal("1"), observed_at=clock.as_of, source=fx_source)
    account = AccountInput(
        currency="GBP",
        live=False,
        simulated=True,
        marked_equity_gbp=anchor.equity_gbp,
        equity_freshness=Freshness.FRESH_VALIDATED,
        baseline_kind=BaselineKind.FIRST_FRESH_OPENING,
        opening_equity_gbp=opening,
        opening_freshness=Freshness.FRESH_VALIDATED,
        opening_london_date=london_date(clock.as_of),
        as_of_london_date=london_date(clock.as_of),
        net_external_deposits_gbp=flows,
        available_cash_gbp=cash_available,
        available_cash_status=cash_status,
        gross_exposure_status=KnownStatus.KNOWN if anchor.exposure_status == "KNOWN" else KnownStatus.UNKNOWN,
        daily_halt_latched=HaltName.DAILY_LOSS.value in active_halts(connection),
        loss_streak_halt_latched=HaltName.LOSS_STREAK.value in active_halts(connection),
    )
    del linked_ids
    assessment = assess_long_entries(
        policy=policy,
        account=account,
        clock=ClockInput(as_of=clock.as_of),
        proposals=(proposal,),
        positions=positions,
        closed_episodes=closed,
        quote=quote,
        fx_quote=fx_quote,
        session=SessionInput(
            calendar_id=str(generation["calendar_id"]),
            session_id=str(generation["session_id"]),
            in_session=True,
        ),
        fixture_limits=limits,
    )
    for finding in assessment.budget_findings:
        reasons.append(finding.reason.value)
    if assessment.supplied_daily_halt_blocks:
        reasons.append(HaltName.DAILY_LOSS.value)
    if assessment.supplied_loss_streak_halt_blocks or assessment.point_in_time_loss_streak_blocks:
        reasons.append(HaltName.LOSS_STREAK.value)
    if not assessment.operational_structure_ok:
        for finding in assessment.operational_findings:
            if finding.reason.value not in {Reason.ALLOWLIST_EMPTY.value, Reason.FIXTURE_NOT_OWNER_APPROVED.value}:
                reasons.append(finding.reason.value)
    if not proposal.instrument_id.startswith("SYNTH-"):
        reasons.append(Reason.ALLOWLIST_EMPTY.value)
    figures = assessment.figures
    if figures is None or not figures.proposal_planned_risk_gbp:
        return reasons, False, proposal, episode_id, None, None, None
    planned = figures.proposal_planned_risk_gbp[0]
    notional = figures.proposal_notional_gbp[0]
    cash = figures.proposal_cash_required_gbp[0]
    floor_row = connection.execute(
        "SELECT floor_gbp FROM episode_risk WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    floor = Decimal("0.00") if floor_row is None else Decimal(str(floor_row["floor_gbp"]))
    already = _ZERO
    for row in holds:
        if str(row["episode_id"]) == episode_id:
            already += Decimal(str(row["risk_gbp"]))
    cap = per_trade_risk_cap(anchor.equity_gbp, policy)
    if floor + already + planned > cap:
        reasons.append(Reason.PER_TRADE_CAP_EXCEEDED.value)
    if anchor.gross_exposure_gbp is None:
        reasons.append(Reason.EXPOSURE_UNKNOWN.value)
    else:
        extra_notional = notional
        for row in holds:
            if int(row["cash_counted_in_ledger"]) == 0:
                extra_notional += Decimal(str(row["notional_gbp"]))
        if anchor.gross_exposure_gbp + extra_notional > anchor.equity_gbp:
            reasons.append("GROSS_EXPOSURE_EXCEEDED")
    if not store_scope_allows(connection):
        reasons.append(Reason.SETTINGS_UNAPPROVED_UNCONFIGURED.value)
    clean = [reason for reason in reasons if reason not in {Reason.ALLOWLIST_EMPTY.value, Reason.FIXTURE_NOT_OWNER_APPROVED.value}]
    # ALLOWLIST_EMPTY stays a denial for non-synthetic instruments. Fixture scope
    # still records the empty operational allowlist, but a SYNTH- id is the only
    # test instrument and it does not enter that allowlist.
    if not proposal.instrument_id.startswith("SYNTH-"):
        clean.append(Reason.ALLOWLIST_EMPTY.value)
    return clean, assessment.mathematical_budget_compliant and not clean, proposal, episode_id, planned, notional, cash


def store_scope_allows(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT synthetic_fixture_scope FROM meta WHERE id = 1").fetchone()
    return row is not None and int(row["synthetic_fixture_scope"]) == 1


def _book(
    anchor: object,
    holds: list[sqlite3.Row],
    generation: sqlite3.Row,
    connection: sqlite3.Connection,
) -> tuple[BookPosition, ...]:
    from sterling.gate2.view import AnchoredLedger

    assert isinstance(anchor, AnchoredLedger)
    rows: list[BookPosition] = []
    evidence = {
        str(row["instrument_id"]): row
        for row in connection.execute(
            "SELECT * FROM holding_evidence WHERE generation_id = ?",
            (str(generation["generation_id"]),),
        )
    }
    for instrument in anchor.open_instruments:
        if instrument.currency != "GBP":
            continue
        quote = evidence.get(instrument.instrument_id)
        if quote is None or quote["stop_price"] is None:
            continue
        entry = (instrument.total_cost_gbp / instrument.quantity).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
        mark = Decimal(str(quote["mark_price"]))
        ask = Decimal(str(quote["ask"]))
        conservative = ask if ask >= mark else mark
        rows.append(
            _position(
                episode_id=instrument.episode_id,
                instrument_id=instrument.instrument_id,
                status=PositionStatus.OPEN,
                quantity=instrument.quantity,
                entry=entry,
                mark=mark,
                stop=Decimal(str(quote["stop_price"])),
                conservative=conservative,
            )
        )
    for hold in holds:
        rows.append(
            _position(
                episode_id=str(hold["episode_id"]),
                instrument_id=str(hold["instrument_id"]),
                status=PositionStatus.PENDING_OPEN,
                quantity=Decimal(str(hold["quantity"])),
                entry=Decimal(str(hold["entry_price"])),
                mark=None,
                stop=Decimal(str(hold["stop_price"])),
                conservative=Decimal(str(hold["conservative_price"])),
            )
        )
    return tuple(rows)


def _position(
    *,
    episode_id: str,
    instrument_id: str,
    status: PositionStatus,
    quantity: Decimal,
    entry: Decimal,
    mark: Decimal | None,
    stop: Decimal,
    conservative: Decimal | None = None,
) -> BookPosition:
    conservative_price = conservative if conservative is not None else (entry if mark is None else mark)
    return BookPosition(
        episode_id=episode_id,
        instrument_id=instrument_id,
        instrument_currency="GBP",
        status=status,
        quantity=quantity,
        quantity_precision=8,
        entry_price=entry,
        mark_price=mark,
        stop_price=stop,
        stop_protection=StopProtection.FULL,
        covered_quantity=quantity,
        fx_rate_to_gbp=Decimal("1"),
        conservative_price=conservative_price,
        conservative_fx_rate_to_gbp=Decimal("1"),
        unbooked_entry_cost_gbp=_ZERO,
        expected_exit_cost_gbp=_ZERO,
        adverse_allowance_gbp=_ZERO,
        booked_entry_fees_gbp=_ZERO,
    )


def _bind_episode(proposal: EntryProposal, anchor: object, holds: list[sqlite3.Row]) -> tuple[str, bool]:
    from sterling.gate2.view import AnchoredLedger

    assert isinstance(anchor, AnchoredLedger)
    for instrument in anchor.open_instruments:
        if instrument.instrument_id == proposal.instrument_id:
            return instrument.episode_id, False
    for hold in holds:
        if str(hold["instrument_id"]) == proposal.instrument_id and str(hold["state"]) in _ACTIVE:
            return str(hold["episode_id"]), False
    return proposal.episode_id, True


def _rebound(proposal: EntryProposal, episode_id: str, opens_new: bool) -> EntryProposal:
    if proposal.episode_id == episode_id and proposal.opens_new_episode == opens_new:
        return proposal
    return EntryProposal(
        proposal_id=proposal.proposal_id,
        episode_id=episode_id,
        instrument_id=proposal.instrument_id,
        instrument_currency=proposal.instrument_currency,
        product=proposal.product,
        side=proposal.side,
        borrowing=proposal.borrowing,
        leverage=proposal.leverage,
        opens_new_episode=opens_new,
        quantity=proposal.quantity,
        quantity_precision=proposal.quantity_precision,
        entry_price=proposal.entry_price,
        stop_price=proposal.stop_price,
        stop_protection=proposal.stop_protection,
        covered_quantity=proposal.covered_quantity,
        fx_rate_to_gbp=proposal.fx_rate_to_gbp,
        conservative_price=proposal.conservative_price,
        conservative_fx_rate_to_gbp=proposal.conservative_fx_rate_to_gbp,
        expected_entry_cost_gbp=proposal.expected_entry_cost_gbp,
        expected_exit_cost_gbp=proposal.expected_exit_cost_gbp,
        adverse_allowance_gbp=proposal.adverse_allowance_gbp,
    )


def _pre_budget_reasons(
    store: Gate2Store,
    connection: sqlite3.Connection,
    request: AdmissionRequest,
    clock: TrustedClock,
    generation: sqlite3.Row,
    anchor: object,
    policy: Stage2Policy,
) -> list[str]:
    del anchor, policy
    reasons: list[str] = []
    halts = active_halts(connection)
    for name in (
        HaltName.DAILY_LOSS,
        HaltName.LOSS_STREAK,
        HaltName.DATA_UNTRUSTED,
        HaltName.INTEGRITY,
        HaltName.RECONCILIATION,
        HaltName.OWNER_PAUSE,
    ):
        if name.value in halts:
            reasons.append(name.value)
    row = connection.execute("SELECT last_clock FROM meta WHERE id = 1").fetchone()
    if row is not None and row["last_clock"] is not None and clock.as_of < datetime.fromisoformat(str(row["last_clock"])):
        reasons.append(Reason.CLOCK_ROLLBACK.value)
        trigger_halt(store, connection, HaltName.DATA_UNTRUSTED, {"clock": "rollback"})
    if str(generation["london_date"]) != london_date(clock.as_of).isoformat():
        reasons.append(Reason.OPENING_NOT_THIS_LONDON_DAY.value)
    fixture = str(generation["fixture_json"])
    if fixture:
        payload = json.loads(fixture)
        max_age = int(payload["quote_max_age_seconds"])
        receipt = datetime.fromisoformat(str(generation["receipt_time"]))
        if clock.as_of - receipt > timedelta(seconds=max_age):
            reasons.append(Reason.QUOTE_STALE.value)
    if not str(request.proposal.instrument_id).startswith("SYNTH-"):
        reasons.append(Reason.ALLOWLIST_EMPTY.value)
    if request.proposal.side != "BUY" or request.proposal.product != "LONG_EQUITY":
        reasons.append(Reason.SIDE_NOT_ALLOWED.value if request.proposal.side != "BUY" else Reason.PRODUCT_NOT_ALLOWED.value)
    return reasons


def _maybe_latch_daily(
    store: Gate2Store,
    connection: sqlite3.Connection,
    clock: TrustedClock,
    generation: sqlite3.Row,
    anchor: object,
    policy: Stage2Policy,
) -> None:
    from sterling.gate2.view import AnchoredLedger

    assert isinstance(anchor, AnchoredLedger)
    day = london_date(clock.as_of)
    baseline = baseline_row(connection, day.isoformat())
    if baseline is None or not generation["admissible"]:
        return
    flows = net_external_since(anchor, event_count=int(baseline["event_count"]), baseline_day=day)
    loss = day_loss_gbp(Decimal(str(baseline["equity_gbp"])), anchor.equity_gbp, flows)
    threshold = daily_loss_threshold_cap(Decimal(str(baseline["equity_gbp"])), policy)
    if loss >= threshold:
        trigger_halt(
            store,
            connection,
            HaltName.DAILY_LOSS,
            {
                "day_loss_gbp": format(loss, "f"),
                "threshold_gbp": format(threshold, "f"),
                "london_date": day.isoformat(),
                "generation_id": str(generation["generation_id"]),
            },
        )


def _persist_denial(
    store: Gate2Store,
    connection: sqlite3.Connection,
    request: AdmissionRequest,
    payload_hash: str,
    reasons: tuple[str, ...],
) -> DecisionResult:
    existing = connection.execute(
        """
        SELECT request_id FROM decisions
        WHERE account_id = ? AND environment = ? AND principal = ? AND request_id = ?
        """,
        (request.account_id, request.environment, request.principal, request.request_id),
    ).fetchone()
    if existing is not None:
        return _result(Outcome.REJECTED, ("DUPLICATE_PAYLOAD",))
    seq = store.append_event(
        connection,
        "DENIAL",
        {"reasons": list(reasons), "request_id": request.request_id},
    )
    _write_decision(connection, request, payload_hash, Outcome.DENIED, reasons, None, seq)
    connection.execute(
        """
        INSERT INTO audit (chain_seq, kind, actor_uid, reason, details, outcome)
        VALUES (?, 'DENY', NULL, ?, ?, 'DENIED')
        """,
        (seq, ",".join(reasons), request.request_id),
    )
    fault_point("before_commit")
    return _result(Outcome.DENIED, reasons, chain_seq=seq)


def _write_decision(
    connection: sqlite3.Connection,
    request: AdmissionRequest,
    payload_hash: str,
    outcome: Outcome,
    reasons: tuple[str, ...],
    reservation_id: str | None,
    seq: int,
) -> None:
    connection.execute(
        """
        INSERT INTO decisions (
            account_id, environment, principal, request_id, payload_hash, outcome,
            reservation_id, reasons, chain_seq
        ) VALUES (?, 'simulated', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request.account_id,
            request.principal,
            request.request_id,
            payload_hash,
            outcome.value,
            reservation_id,
            json.dumps(list(reasons)),
            seq,
        ),
    )


def _ledger_path(store: Gate2Store, generation: sqlite3.Row) -> str:
    reload_ledger_paths(store)
    mapped = ledger_path(str(generation["generation_id"]))
    if mapped is None:
        raise StoreBlocked("LEDGER_PATH_NOT_RETAINED")
    return mapped


def _limits_from_json(text: str) -> FixtureOperationalLimits | None:
    if text == "":
        return None
    payload = json.loads(text)
    return FixtureOperationalLimits(
        owner_approved=False,
        label="NOT_OWNER_APPROVED_FIXTURE",
        quote_max_age_seconds=int(payload["quote_max_age_seconds"]),
        fx_max_age_seconds=int(payload["fx_max_age_seconds"]),
        quote_sources=frozenset(payload["quote_sources"]),
        fx_sources=frozenset(payload["fx_sources"]),
        quality_labels=frozenset(payload["quality_labels"]),
        calendar_id=str(payload["calendar_id"]),
        session_id=str(payload["session_id"]),
    )


def _quote_for(connection: sqlite3.Connection, generation: sqlite3.Row, instrument_id: str) -> QuoteInput | None:
    row = connection.execute(
        """
        SELECT * FROM holding_evidence
        WHERE generation_id = ? AND instrument_id = ?
        """,
        (str(generation["generation_id"]), instrument_id),
    ).fetchone()
    if row is None:
        return None
    return QuoteInput(
        bid=Decimal(str(row["bid"])),
        ask=Decimal(str(row["ask"])),
        observed_at=datetime.fromisoformat(str(row["observed_at"])),
        source=str(row["source"]),
        quality=str(row["quality"]),
    )


def _publish(store: Gate2Store, result: DecisionResult) -> DecisionResult:
    fault_point("after_commit_before_reply")
    try:
        store.publish_checkpoint()
    except StoreUnavailable as exc:
        return DecisionResult(
            outcome=result.outcome,
            reasons=(*result.reasons, "CHECKPOINT_LAG", exc.reason),
            reservation_id=result.reservation_id,
            reservation_state=result.reservation_state,
            replayed=result.replayed,
            chain_seq=result.chain_seq,
        )
    return result


def _result(
    outcome: Outcome,
    reasons: tuple[str, ...],
    reservation_id: str | None = None,
    reservation_state: str | None = None,
    *,
    replayed: bool = False,
    chain_seq: int | None = None,
) -> DecisionResult:
    return DecisionResult(
        outcome=outcome,
        reasons=reasons,
        reservation_id=reservation_id,
        reservation_state=reservation_state,
        replayed=replayed,
        chain_seq=chain_seq,
    )

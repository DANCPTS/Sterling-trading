"""Owner reset. The peer identity comes from SO_PEERCRED, not from the request.

A reset clears one named reason. It does not clear the others, does not
issue a ticket, and does not bypass budgets. Independent checks run again
under the writer lock. An authenticated owner is not proof that the account
is safe.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from sterling.gate2.canonical import sha256_bytes
from sterling.gate2.errors import StoreBlocked, StoreUnavailable
from sterling.gate2.faults import fault_point
from sterling.gate2.models import DecisionResult, HaltName, Outcome, ResetRequest, TrustedClock
from sterling.gate2.paths import reload_ledger_paths
from sterling.gate2.peer import PeerIdentity
from sterling.gate2.state import active_halts, baseline_row, episode_history_hash, resolve_halt, trigger_halt
from sterling.gate2.store import Gate2Store
from sterling.gate2.view import load_anchored_ledger, net_external_since
from sterling.ledger.calendar import london_date
from sterling.risk.budget import daily_loss_threshold_cap, day_loss_gbp
from sterling.risk.policy import Stage2Policy, load_default_policy
from sterling.risk.reasons import Reason

_ZERO = Decimal("0.00")


def apply_reset(
    store: Gate2Store,
    request: ResetRequest,
    clock: TrustedClock,
    peer: PeerIdentity,
    *,
    policy: Stage2Policy | None = None,
) -> DecisionResult:
    """Apply or refuse one scoped reset. The nonce cannot reset twice."""
    if peer.source != "SO_PEERCRED":
        return _decision(Outcome.DENIED, ("RESET_PEER_UNVERIFIED",))
    if peer.uid != store.identity.owner_uid:
        return _decision(Outcome.DENIED, (Reason.RESET_ACTOR_NOT_OWNER.value,))
    if peer.uid == store.identity.ai_uid:
        return _decision(Outcome.DENIED, (Reason.RESET_ACTOR_AI.value,))
    loaded = policy if policy is not None else load_default_policy()
    try:
        with store.immediate() as connection:
            fault_point("reset_before_commit")
            result = _reset_locked(store, connection, request, clock, peer, loaded)
        fault_point("reset_after_commit_before_checkpoint")
        try:
            store.publish_checkpoint()
        except StoreUnavailable as exc:
            return _decision(Outcome.UNAVAILABLE, (*result.reasons, "CHECKPOINT_LAG", exc.reason))
        if result.outcome != Outcome.DENIED or "RESET_APPLIED" in result.reasons:
            return result
        return result
    except StoreBlocked as exc:
        return _decision(Outcome.BLOCKED, (exc.reason,))
    except (StoreUnavailable, OSError, sqlite3.OperationalError) as exc:
        return _decision(Outcome.UNAVAILABLE, (str(exc),))


def _reset_locked(
    store: Gate2Store,
    connection: sqlite3.Connection,
    request: ResetRequest,
    clock: TrustedClock,
    peer: PeerIdentity,
    policy: Stage2Policy,
) -> DecisionResult:
    if request.account_id != store.identity.account_id or request.environment != "simulated":
        return _audit_reject(store, connection, request, peer, ("ACCOUNT_OR_ENVIRONMENT_REJECTED",), consume=False)
    if not isinstance(request.reason, str) or request.reason.strip() == "":
        return _audit_reject(store, connection, request, peer, (Reason.RESET_REASON_MISSING.value,), consume=False)
    if request.expected_policy_hash != store.identity.policy_hash or request.expected_policy_hash != policy.content_hash:
        return _audit_reject(store, connection, request, peer, ("POLICY_HASH_MISMATCH",), consume=True)
    if clock.as_of > request.expires_at:
        return _audit_reject(store, connection, request, peer, ("NONCE_EXPIRED",), consume=True)
    nonce_hash = sha256_bytes(request.nonce.encode("utf-8"))
    prior = connection.execute("SELECT * FROM nonces WHERE nonce_hash = ?", (nonce_hash,)).fetchone()
    if prior is not None:
        return DecisionResult(
            outcome=Outcome(str(prior["outcome"])),
            reasons=tuple(json.loads(str(prior["details"]))),
            reservation_id=None,
            reservation_state=None,
            replayed=True,
            chain_seq=int(prior["chain_seq"]),
        )
    head_seq, _hash = store.head()
    if request.expected_chain_seq != head_seq:
        return _audit_reject(store, connection, request, peer, ("STALE_GENERATION",), consume=True)
    current = connection.execute("SELECT generation_id, admissible FROM current_generation WHERE id = 1").fetchone()
    if current is None or str(current["generation_id"] or "") != request.expected_generation_id or not current["admissible"]:
        return _audit_reject(store, connection, request, peer, ("STALE_GENERATION",), consume=True)
    halts = active_halts(connection)
    if request.halt_reason.value not in halts:
        return _audit_reject(store, connection, request, peer, ("HALT_NOT_ACTIVE",), consume=True)
    generation = connection.execute(
        "SELECT * FROM generations WHERE generation_id = ?",
        (request.expected_generation_id,),
    ).fetchone()
    if generation is None:
        return _audit_reject(store, connection, request, peer, ("GENERATION_MISSING",), consume=True)
    problem = _independent(store, connection, request, clock, policy, generation, halts)
    if problem is not None:
        return _audit_reject(store, connection, request, peer, (problem,), consume=True)
    evidence: dict[str, object] = {
        "halt": request.halt_reason.value,
        "reason": request.reason.strip(),
        "target": request.target_london_date,
        "uid": peer.uid,
    }
    resolve_halt(store, connection, request.halt_reason, evidence, action="RESET")
    if request.halt_reason == HaltName.LOSS_STREAK:
        _open_epoch(store, connection, generation)
    seq = store.append_event(connection, "RESET", evidence)
    _store_nonce(connection, nonce_hash, request, Outcome.DENIED, ("RESET_APPLIED",), seq)
    connection.execute(
        """
        INSERT INTO audit (chain_seq, kind, actor_uid, reason, details, outcome)
        VALUES (?, 'RESET', ?, ?, ?, 'RESET_APPLIED')
        """,
        (seq, peer.uid, request.reason.strip(), request.halt_reason.value),
    )
    fault_point("reset_audit_fail")
    return _decision(Outcome.DENIED, ("RESET_APPLIED",), chain_seq=seq)


def _independent(
    store: Gate2Store,
    connection: sqlite3.Connection,
    request: ResetRequest,
    clock: TrustedClock,
    policy: Stage2Policy,
    generation: sqlite3.Row,
    halts: dict[str, dict[str, object]],
) -> str | None:
    reload_ledger_paths(store)
    from pathlib import Path

    from sterling.gate2.paths import ledger_path

    path = ledger_path(str(generation["generation_id"]))
    if path is None:
        return "LEDGER_PATH_NOT_RETAINED"
    try:
        anchor = load_anchored_ledger(Path(path))
    except StoreBlocked:
        return "LEDGER_UNREADABLE"
    if anchor.file_sha256 != str(generation["ledger_sha256"]):
        return "LEDGER_HASH_MISMATCH"
    if anchor.exposure_status != "KNOWN":
        return Reason.EXPOSURE_UNKNOWN.value
    row = connection.execute("SELECT last_clock FROM meta WHERE id = 1").fetchone()
    if row is not None and row["last_clock"] is not None and clock.as_of < datetime.fromisoformat(str(row["last_clock"])):
        return Reason.CLOCK_ROLLBACK.value
    if request.halt_reason == HaltName.DAILY_LOSS:
        return _daily_checks(connection, request, clock, policy, generation, anchor, halts)
    if request.halt_reason == HaltName.LOSS_STREAK:
        return None
    if request.halt_reason == HaltName.OWNER_PAUSE:
        return None
    if request.halt_reason == HaltName.DATA_UNTRUSTED:
        if not generation["admissible"]:
            return "DATA_STILL_UNTRUSTED"
        return None
    if request.halt_reason in {HaltName.INTEGRITY, HaltName.RECONCILIATION}:
        if not request.reviewed_reconciliation:
            return "RECONCILIATION_NOT_REVIEWED"
        return None
    return "RESET_UNSUPPORTED"


def _daily_checks(
    connection: sqlite3.Connection,
    request: ResetRequest,
    clock: TrustedClock,
    policy: Stage2Policy,
    generation: sqlite3.Row,
    anchor: object,
    halts: dict[str, dict[str, object]],
) -> str | None:
    from sterling.gate2.view import AnchoredLedger

    assert isinstance(anchor, AnchoredLedger)
    if request.target_london_date is None:
        return Reason.RESET_DAILY_REQUIRES_TRADING_DAY_TRANSITION.value
    evidence = halts[HaltName.DAILY_LOSS.value]["evidence"]
    assert isinstance(evidence, dict)
    latch_day = str(evidence.get("london_date", ""))
    if request.target_london_date <= latch_day:
        return Reason.RESET_DAILY_REQUIRES_TRADING_DAY_TRANSITION.value
    if str(generation["london_date"]) != request.target_london_date:
        return Reason.RESET_DAILY_REQUIRES_TRADING_DAY_TRANSITION.value
    if london_date(clock.as_of).isoformat() != request.target_london_date:
        return Reason.CLOCK_DATE_MISMATCH.value
    if not generation["verified_trading_day"]:
        return Reason.SESSION_NOT_CONFIRMED.value
    baseline = baseline_row(connection, request.target_london_date)
    if baseline is None:
        return Reason.RESET_DAILY_REQUIRES_FRESH_ACCOUNT.value
    opening = Decimal(str(baseline["equity_gbp"]))
    flows = net_external_since(
        anchor,
        event_count=int(baseline["event_count"]),
        baseline_day=london_date(clock.as_of),
    )
    loss = day_loss_gbp(opening, anchor.equity_gbp, flows)
    threshold = daily_loss_threshold_cap(opening, policy)
    if loss >= threshold:
        return Reason.DAY_LOSS_AT_OR_ABOVE_THRESHOLD.value
    return None


def _open_epoch(store: Gate2Store, connection: sqlite3.Connection, generation: sqlite3.Row) -> None:
    from sterling.gate2.view import FlatEpisode

    rows = connection.execute("SELECT * FROM episode_cache ORDER BY close_index, close_event_id").fetchall()
    episodes = tuple(
        FlatEpisode(
            episode_id=str(row["episode_id"]),
            instrument_id=str(row["instrument_id"]),
            origin_fill_id=str(row["origin_fill_id"]),
            close_event_id=str(row["close_event_id"]),
            close_index=int(row["close_index"]),
            net_gbp=Decimal(str(row["net_gbp"])),
            component_count=int(row["component_count"]),
        )
        for row in rows
    )
    acknowledged = [
        {
            "close_event_id": episode.close_event_id,
            "episode_id": episode.episode_id,
            "net_gbp": format(episode.net_gbp, "f"),
        }
        for episode in episodes
    ]
    connection.execute("UPDATE streak_epochs SET active = 0 WHERE active = 1")
    digest = episode_history_hash(episodes)
    seq = store.append_event(
        connection,
        "STREAK_EPOCH",
        {"generation_id": str(generation["generation_id"]), "history_hash": digest},
    )
    connection.execute(
        """
        INSERT INTO streak_epochs (epoch_id, chain_seq, history_hash, acknowledged_json, active)
        VALUES (?, ?, ?, ?, 1)
        """,
        (f"epoch-{seq}", seq, digest, json.dumps(acknowledged)),
    )


def _audit_reject(
    store: Gate2Store,
    connection: sqlite3.Connection,
    request: ResetRequest,
    peer: PeerIdentity,
    reasons: tuple[str, ...],
    *,
    consume: bool,
) -> DecisionResult:
    seq = store.append_event(connection, "RESET_REJECTED", {"reasons": list(reasons), "halt": request.halt_reason.value})
    connection.execute(
        """
        INSERT INTO audit (chain_seq, kind, actor_uid, reason, details, outcome)
        VALUES (?, 'RESET_REJECTED', ?, ?, ?, 'REJECTED')
        """,
        (seq, peer.uid, request.reason if isinstance(request.reason, str) else "", ",".join(reasons)),
    )
    if consume and isinstance(request.nonce, str) and request.nonce != "":
        _store_nonce(connection, sha256_bytes(request.nonce.encode("utf-8")), request, Outcome.DENIED, reasons, seq)
    return _decision(Outcome.DENIED, reasons, chain_seq=seq)


def _store_nonce(
    connection: sqlite3.Connection,
    nonce_hash: str,
    request: ResetRequest,
    outcome: Outcome,
    reasons: tuple[str, ...],
    seq: int,
) -> None:
    request_hash = sha256_bytes(
        json.dumps(
            {
                "generation": request.expected_generation_id,
                "halt": request.halt_reason.value,
                "target": request.target_london_date,
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    connection.execute(
        """
        INSERT INTO nonces (nonce_hash, request_hash, outcome, details, chain_seq, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (nonce_hash, request_hash, outcome.value, json.dumps(list(reasons)), seq, request.expires_at.isoformat()),
    )


def owner_pause(
    store: Gate2Store,
    clock: TrustedClock,
    peer: PeerIdentity,
    *,
    reason: str,
) -> DecisionResult:
    """Latch OWNER_PAUSE. This does not clear a loss halt and does not trade."""
    del clock
    if peer.source != "SO_PEERCRED" or peer.uid != store.identity.owner_uid:
        return _decision(Outcome.DENIED, (Reason.RESET_ACTOR_NOT_OWNER.value,))
    if reason.strip() == "":
        return _decision(Outcome.DENIED, (Reason.RESET_REASON_MISSING.value,))
    try:
        with store.immediate() as connection:
            trigger_halt(store, connection, HaltName.OWNER_PAUSE, {"reason": reason.strip(), "uid": peer.uid})
            seq = store.append_event(connection, "OWNER_PAUSE", {"reason": reason.strip()})
            connection.execute(
                """
                INSERT INTO audit (chain_seq, kind, actor_uid, reason, details, outcome)
                VALUES (?, 'OWNER_PAUSE', ?, ?, '', 'TRIGGER')
                """,
                (seq, peer.uid, reason.strip()),
            )
        try:
            store.publish_checkpoint()
        except StoreUnavailable as exc:
            return _decision(Outcome.UNAVAILABLE, ("CHECKPOINT_LAG", exc.reason))
        return _decision(Outcome.DENIED, ("OWNER_PAUSE_LATCHED",))
    except StoreBlocked as exc:
        return _decision(Outcome.BLOCKED, (exc.reason,))
    except StoreUnavailable as exc:
        return _decision(Outcome.UNAVAILABLE, (exc.reason,))


def _decision(outcome: Outcome, reasons: tuple[str, ...], *, chain_seq: int | None = None) -> DecisionResult:
    return DecisionResult(
        outcome=outcome,
        reasons=reasons,
        reservation_id=None,
        reservation_state=None,
        replayed=False,
        chain_seq=chain_seq,
    )

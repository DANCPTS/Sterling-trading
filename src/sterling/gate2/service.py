"""Local Unix sockets for proposals, ingress, and owner control.

There is no daemon install, no remote listener, and no simulator socket.
Peer identity is SO_PEERCRED. A JSON field cannot change it.
"""

from __future__ import annotations

import json
import os
import select
import socket
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sterling.gate2.admit import admit, lookup, release_never_exported
from sterling.gate2.errors import Gate2Error, ProtocolError
from sterling.gate2.ingress import ingest
from sterling.gate2.models import (
    AdmissionRequest,
    FxEvidence,
    GenerationInput,
    HaltName,
    HoldingQuote,
    ResetRequest,
    StopEvidence,
    TrustedClock,
    reject_generation_document,
)
from sterling.gate2.owner import apply_reset, owner_pause
from sterling.gate2.peer import peer_from_socket
from sterling.gate2.store import Gate2Store, open_store
from sterling.risk.contracts import EntryProposal, FixtureOperationalLimits, StopProtection

_MAX_BYTES = 65536
_PROPOSAL_OPS = frozenset({"propose", "lookup", "status", "protective_check"})
_INGRESS_OPS = frozenset({"ingest", "release"})
_OWNER_OPS = frozenset({"reset", "pause", "reconcile", "status"})


def serve(
    root: Path,
    *,
    proposal_path: Path,
    owner_path: Path,
    ingress_path: Path,
    stop_file: Path,
    owner_anchor: Path | None = None,
) -> None:
    """Serve until ``stop_file`` appears. The process must be the risk uid when separated."""
    store = open_store(root, owner_anchor=owner_anchor)
    if store.identity.os_identity_separated and os.geteuid() != store.identity.risk_uid:
        store.close()
        raise SystemExit("service must run as the risk uid")
    proposal = _bind(proposal_path, mode=0o660)
    owner = _bind(owner_path, mode=0o660)
    ingress = _bind(ingress_path, mode=0o660)
    if store.identity.os_identity_separated:
        _chgrp(proposal_path, store.identity.ai_uid)
        _chgrp(owner_path, store.identity.owner_uid)
        _chgrp(ingress_path, store.identity.ingress_uid)
    sockets = {proposal: "proposal", owner: "owner", ingress: "ingress"}
    try:
        while not stop_file.exists():
            readable, _, _ = select.select(list(sockets), [], [], 0.2)
            for server in readable:
                connection, _addr = server.accept()
                try:
                    _handle(store, connection, sockets[server])
                finally:
                    connection.close()
    finally:
        for server in sockets:
            server.close()
        store.close()


def _chgrp(path: Path, uid: int) -> None:
    """Give the named principal's primary group access. Requires membership."""
    import pwd

    gid = pwd.getpwuid(uid).pw_gid
    os.chown(path, -1, gid)


def _bind(path: Path, *, mode: int) -> socket.socket:
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, mode)
    server.listen(8)
    return server


def _handle(store: Gate2Store, connection: socket.socket, channel: str) -> None:
    peer = peer_from_socket(connection)
    raw = _read(connection)
    if raw is None:
        _send(connection, {"ok": False, "reasons": ["PAYLOAD_TOO_LARGE"], "execution_permission_issued": False})
        return
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        _send(connection, {"ok": False, "reasons": ["MALFORMED"], "execution_permission_issued": False})
        return
    if not isinstance(document, dict):
        _send(connection, {"ok": False, "reasons": ["MALFORMED"], "execution_permission_issued": False})
        return
    op = document.get("op")
    if op in {"sql", "policy_override", "execute", "ticket"}:
        _send(
            connection,
            {
                "ok": False,
                "reasons": ["OPERATION_DENIED"],
                "execution_permission_issued": False,
                "ticket_issued": False,
            },
        )
        return
    if channel == "proposal" and op not in _PROPOSAL_OPS:
        _send(connection, {"ok": False, "reasons": ["PROPOSAL_CHANNEL_DENIED"], "execution_permission_issued": False})
        return
    if channel == "owner" and peer.uid != store.identity.owner_uid:
        _send(connection, {"ok": False, "reasons": ["OWNER_UID_REJECTED"], "execution_permission_issued": False})
        return
    if channel == "ingress" and peer.uid not in {store.identity.ingress_uid, store.identity.risk_uid}:
        _send(connection, {"ok": False, "reasons": ["INGRESS_UID_REJECTED"], "execution_permission_issued": False})
        return
    if channel == "owner" and op not in _OWNER_OPS:
        _send(connection, {"ok": False, "reasons": ["OWNER_CHANNEL_DENIED"], "execution_permission_issued": False})
        return
    if channel == "ingress" and op not in _INGRESS_OPS:
        _send(connection, {"ok": False, "reasons": ["INGRESS_CHANNEL_DENIED"], "execution_permission_issued": False})
        return
    limit = int(os.environ.get("STERLING_GATE2_MAX_REQUESTS", "1000"))
    count = _counts.get(peer.uid, 0) + 1
    _counts[peer.uid] = count
    if count > limit:
        _send(connection, {"ok": False, "reasons": ["RATE_LIMIT"], "execution_permission_issued": False})
        return
    clock = _clock(document.get("clock"))
    if op == "status":
        _send(connection, _status(store))
        return
    if op == "propose":
        result = admit(store, _proposal(document), clock)
        _send(connection, _dump(result))
        return
    if op == "lookup":
        result = lookup(
            store,
            account_id=str(document["account_id"]),
            environment=str(document["environment"]),
            principal=str(document["principal"]),
            request_id=str(document["request_id"]),
        )
        _send(connection, _dump(result))
        return
    if op == "reset":
        result = apply_reset(store, _reset(document), clock, peer)
        _send(connection, _dump(result))
        return
    if op == "pause":
        result = owner_pause(store, clock, peer, reason=str(document.get("reason", "")))
        _send(connection, _dump(result))
        return
    if op == "reconcile":
        try:
            checkpoint = store.reconcile_checkpoint()
            _send(
                connection,
                {
                    "ok": True,
                    "reasons": ["CHECKPOINT_RECONCILED"],
                    "sequence": checkpoint.sequence,
                    "execution_permission_issued": False,
                    "ticket_issued": False,
                },
            )
        except (Gate2Error, OSError) as exc:
            _send(connection, {"ok": False, "reasons": [str(exc)], "execution_permission_issued": False})
        return
    if op == "release":
        result = release_never_exported(
            store,
            str(document["reservation_id"]),
            peer_uid=peer.uid,
            proof=str(document.get("proof", "")),
        )
        _send(connection, _dump(result))
        return
    if op == "ingest":
        try:
            reject_generation_document(document)
            result = ingest(store, _generation(document), clock, peer_uid=peer.uid)
        except ProtocolError as exc:
            _send(connection, {"ok": False, "reasons": [str(exc)], "execution_permission_issued": False, "ticket_issued": False})
            return
        _send(connection, _dump(result))
        return
    if op == "protective_check":
        _send(
            connection,
            {
                "ok": True,
                "reasons": ["NO_EXECUTION_PATH"],
                "executed": False,
                "execution_permission_issued": False,
                "ticket_issued": False,
            },
        )
        return
    _send(connection, {"ok": False, "reasons": ["UNKNOWN_OP"], "execution_permission_issued": False})


_counts: dict[int, int] = {}


def _read(connection: socket.socket) -> str | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        piece = connection.recv(4096)
        if not piece:
            break
        total += len(piece)
        if total > _MAX_BYTES:
            return None
        chunks.append(piece)
        if b"\n" in piece:
            break
    return b"".join(chunks).split(b"\n", 1)[0].decode("utf-8")


def _send(connection: socket.socket, payload: dict[str, object]) -> None:
    body = dict(payload)
    body.setdefault("execution_permission_issued", False)
    body.setdefault("ticket_issued", False)
    body.setdefault("token", None)
    connection.sendall(json.dumps(body, sort_keys=True).encode("utf-8") + b"\n")


def _status(store: Gate2Store) -> dict[str, object]:
    head = None
    try:
        seq, link = store.head()
        head = {"sequence": seq, "chain_hash": link}
    except Exception as exc:
        head = {"error": str(exc)}
    return {
        "ok": True,
        "blocked": store.blocked,
        "head": head,
        "separated": store.identity.os_identity_separated,
        "fixture_scope": store.identity.synthetic_fixture_scope,
        "executor_present": False,
        "execution_permission_issued": False,
        "ticket_issued": False,
        "stage1_jsonl_atomic_with_risk": False,
    }


def _dump(result: object) -> dict[str, object]:
    from sterling.gate2.models import DecisionResult

    assert isinstance(result, DecisionResult)
    return {
        "ok": result.outcome.value != "UNAVAILABLE",
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "reservation_id": result.reservation_id,
        "reservation_state": result.reservation_state,
        "replayed": result.replayed,
        "execution_permission_issued": False,
        "ticket_issued": False,
        "token": None,
        "non_executable": True,
    }


def _clock(value: object) -> TrustedClock:
    if not isinstance(value, dict):
        raise ProtocolError("clock is required")
    as_of = datetime.fromisoformat(str(value["as_of"]))
    return TrustedClock(as_of=as_of, monotonic_ns=int(value["monotonic_ns"]))


def _proposal(document: dict[str, Any]) -> AdmissionRequest:
    body = document["proposal"]
    if not isinstance(body, dict):
        raise ProtocolError("proposal must be an object")
    proposal = EntryProposal(
        proposal_id=str(body["proposal_id"]),
        episode_id=str(body["episode_id"]),
        instrument_id=str(body["instrument_id"]),
        instrument_currency=str(body["instrument_currency"]),
        product=str(body["product"]),
        side=str(body["side"]),
        borrowing=bool(body["borrowing"]),
        leverage=Decimal(str(body["leverage"])),
        opens_new_episode=bool(body["opens_new_episode"]),
        quantity=Decimal(str(body["quantity"])),
        quantity_precision=int(body["quantity_precision"]),
        entry_price=Decimal(str(body["entry_price"])),
        stop_price=None if body.get("stop_price") is None else Decimal(str(body["stop_price"])),
        stop_protection=StopProtection(str(body["stop_protection"])),
        covered_quantity=None if body.get("covered_quantity") is None else Decimal(str(body["covered_quantity"])),
        fx_rate_to_gbp=Decimal(str(body["fx_rate_to_gbp"])),
        conservative_price=Decimal(str(body["conservative_price"])),
        conservative_fx_rate_to_gbp=Decimal(str(body["conservative_fx_rate_to_gbp"])),
        expected_entry_cost_gbp=Decimal(str(body["expected_entry_cost_gbp"])),
        expected_exit_cost_gbp=Decimal(str(body["expected_exit_cost_gbp"])),
        adverse_allowance_gbp=Decimal(str(body["adverse_allowance_gbp"])),
    )
    override = document.get("policy_override") or {}
    pairs = tuple((str(key), str(value)) for key, value in override.items()) if isinstance(override, dict) else ()
    return AdmissionRequest(
        request_id=str(document["request_id"]),
        principal=str(document["principal"]),
        account_id=str(document["account_id"]),
        environment=str(document["environment"]),
        proposal=proposal,
        claimed_owner=bool(document.get("claimed_owner", False)),
        claimed_approved=bool(document.get("claimed_approved", False)),
        claimed_execution=bool(document.get("claimed_execution", False)),
        policy_override=pairs,
        signature=None if document.get("signature") in {None, ""} else str(document.get("signature")),
    )


def _reset(document: dict[str, Any]) -> ResetRequest:
    return ResetRequest(
        account_id=str(document["account_id"]),
        environment=str(document["environment"]),
        halt_reason=HaltName(str(document["halt_reason"])),
        reason=str(document.get("reason", "")),
        target_london_date=None if document.get("target_london_date") is None else str(document["target_london_date"]),
        expected_generation_id=str(document["expected_generation_id"]),
        expected_policy_hash=str(document["expected_policy_hash"]),
        expected_chain_seq=int(document["expected_chain_seq"]),
        nonce=str(document["nonce"]),
        expires_at=datetime.fromisoformat(str(document["expires_at"])),
        reviewed_reconciliation=bool(document.get("reviewed_reconciliation", False)),
    )


def _generation(document: dict[str, Any]) -> GenerationInput:
    fixture = document.get("fixture_limits")
    limits = None
    if fixture is not None:
        if not isinstance(fixture, dict):
            raise ProtocolError("fixture_limits must be an object")
        limits = FixtureOperationalLimits(
            owner_approved=False,
            label="NOT_OWNER_APPROVED_FIXTURE",
            quote_max_age_seconds=int(fixture["quote_max_age_seconds"]),
            fx_max_age_seconds=int(fixture["fx_max_age_seconds"]),
            quote_sources=frozenset(fixture["quote_sources"]),
            fx_sources=frozenset(fixture["fx_sources"]),
            quality_labels=frozenset(fixture["quality_labels"]),
            calendar_id=str(fixture["calendar_id"]),
            session_id=str(fixture["session_id"]),
        )
    quotes: dict[str, HoldingQuote] = {}
    for instrument_id, quote in dict(document.get("quotes", {})).items():
        quotes[str(instrument_id)] = HoldingQuote(
            bid=Decimal(str(quote["bid"])),
            ask=Decimal(str(quote["ask"])),
            mark=Decimal(str(quote["mark"])),
            observed_at=datetime.fromisoformat(str(quote["observed_at"])),
            source=str(quote["source"]),
            quality=str(quote["quality"]),
        )
    fx: dict[str, FxEvidence] = {}
    for currency, rate in dict(document.get("fx", {})).items():
        fx[str(currency)] = FxEvidence(
            rate_to_gbp=Decimal(str(rate["rate_to_gbp"])),
            observed_at=datetime.fromisoformat(str(rate["observed_at"])),
            source=str(rate["source"]),
        )
    stops = {
        str(instrument_id): StopEvidence(
            stop_price=Decimal(str(stop["stop_price"])),
            covered_quantity=Decimal(str(stop["covered_quantity"])),
        )
        for instrument_id, stop in dict(document.get("holding_stops", {})).items()
    }
    pending = {
        str(fill_id): StopEvidence(
            stop_price=Decimal(str(stop["stop_price"])),
            covered_quantity=Decimal(str(stop["covered_quantity"])),
        )
        for fill_id, stop in dict(document.get("pending_stops", {})).items()
    }
    return GenerationInput(
        source_id=str(document["source_id"]),
        source_sequence=int(document["source_sequence"]),
        ledger_path=str(document["ledger_path"]),
        as_of=datetime.fromisoformat(str(document["as_of"])),
        receipt_time=datetime.fromisoformat(str(document["receipt_time"])),
        calendar_id=str(document["calendar_id"]),
        calendar_version=str(document["calendar_version"]),
        session_id=str(document["session_id"]),
        in_session=bool(document["in_session"]),
        verified_trading_day=bool(document["verified_trading_day"]),
        is_opening_snapshot=bool(document["is_opening_snapshot"]),
        opening_claim=str(document["opening_claim"]),
        quotes=quotes,
        fx=fx,
        holding_stops=stops,
        pending_stops=pending,
        reported_cash_gbp=None if document.get("reported_cash_gbp") is None else Decimal(str(document["reported_cash_gbp"])),
        reported_equity_gbp=None if document.get("reported_equity_gbp") is None else Decimal(str(document["reported_equity_gbp"])),
        reported_exposure_gbp=None
        if document.get("reported_exposure_gbp") is None
        else Decimal(str(document["reported_exposure_gbp"])),
        costs_final=bool(document.get("costs_final", True)),
        ambiguous_fill_ids=tuple(str(item) for item in document.get("ambiguous_fill_ids", [])),
        fixture_limits=limits,
    )

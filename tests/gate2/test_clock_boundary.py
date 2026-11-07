"""IPC callers cannot supply the coordinator clock."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from tests.gate2.peers import Coordinator, send
from tests.gate2.support import MOMENT, anchor, fresh_book, new_store, request
from tests.support import june

from sterling.gate2.admit import admit
from sterling.gate2.models import HaltName
from sterling.gate2.state import active_halts
from sterling.gate2.store import open_store

FUTURE = june(1, 10).replace(year=2030)
PAST = june(1, 10).replace(year=2020)
DAY2 = june(2, 10)


def test_proposal_clock_cannot_freshen_or_move_the_day(tmp_path: Path) -> None:
    uid = os.getuid()
    store = new_store(tmp_path, owner_uid=uid, ingress_uid=uid, risk_uid=uid, ai_uid=uid + 4)
    book = fresh_book(tmp_path / "book.jsonl")
    assert "GENERATION_ANCHORED" in anchor(store, book, tmp_path / "book.jsonl").reasons
    before = _snapshot(store)
    root = store.root
    policy_hash = store.identity.policy_hash
    generation_id = str(store.connection.execute("SELECT generation_id FROM current_generation").fetchone()["generation_id"])
    store.close()

    coordinator = Coordinator(root, tmp_path / "socks")
    coordinator.start()
    try:
        forged_future = _propose(coordinator, "future", FUTURE)
        honest = _propose(coordinator, "future", MOMENT)
        assert forged_future["outcome"] == honest["outcome"]
        assert honest.get("replayed") is True
        assert forged_future["execution_permission_issued"] is False
        coordinator.clock.set(MOMENT + timedelta(days=3))
        denied = _propose(coordinator, "stale", MOMENT)
        assert "QUOTE_STALE" in denied["reasons"] or "OPENING_NOT_THIS_LONDON_DAY" in denied["reasons"]
        assert denied["reservation_id"] is None
        coordinator.clock.set(MOMENT)
        rollback = _propose(coordinator, "past", PAST)
        assert rollback["execution_permission_issued"] is False
        assert "CLOCK_ROLLBACK" not in rollback["reasons"]
        day_shift = send(
            coordinator.owner,
            _reset_body(
                reason="forged clock must not open the next London day",
                target="2026-06-02",
                generation_id=generation_id,
                policy_hash=policy_hash,
                sequence=_sequence(coordinator),
                nonce="forged-day",
                moment=DAY2,
            ),
        )
        assert "RESET_APPLIED" not in day_shift["reasons"]
        assert day_shift["execution_permission_issued"] is False
    finally:
        coordinator.shutdown()

    reopened = open_store(root)
    after = _snapshot(reopened)
    assert after["last_clock"].startswith("2026-06-01")
    assert HaltName.DAILY_LOSS.value not in active_halts(reopened.connection)
    assert HaltName.DATA_UNTRUSTED.value not in active_halts(reopened.connection)
    success = reopened.connection.execute("SELECT COUNT(*) AS n FROM audit WHERE outcome = 'RESET_APPLIED'").fetchone()
    assert success is not None and int(success["n"]) == 0
    assert after["reservations"] == before["reservations"] or after["reservations"] == before["reservations"] + 1
    reopened.close()


def _propose(coordinator: Coordinator, name: str, forged: object) -> dict[str, object]:
    from datetime import datetime

    assert isinstance(forged, datetime)
    body = request(name)
    proposal = body.proposal
    return send(
        coordinator.proposal,
        {
            "op": "propose",
            "request_id": body.request_id,
            "principal": body.principal,
            "account_id": body.account_id,
            "environment": body.environment,
            "clock": {"as_of": forged.isoformat(), "monotonic_ns": 1},
            "proposal": {
                "proposal_id": proposal.proposal_id,
                "episode_id": proposal.episode_id,
                "instrument_id": proposal.instrument_id,
                "instrument_currency": proposal.instrument_currency,
                "product": proposal.product,
                "side": proposal.side,
                "borrowing": proposal.borrowing,
                "leverage": format(proposal.leverage, "f"),
                "opens_new_episode": proposal.opens_new_episode,
                "quantity": format(proposal.quantity, "f"),
                "quantity_precision": proposal.quantity_precision,
                "entry_price": format(proposal.entry_price, "f"),
                "stop_price": format(proposal.stop_price, "f") if proposal.stop_price is not None else None,
                "stop_protection": proposal.stop_protection.value,
                "covered_quantity": format(proposal.covered_quantity, "f") if proposal.covered_quantity is not None else None,
                "fx_rate_to_gbp": format(proposal.fx_rate_to_gbp, "f"),
                "conservative_price": format(proposal.conservative_price, "f"),
                "conservative_fx_rate_to_gbp": format(proposal.conservative_fx_rate_to_gbp, "f"),
                "expected_entry_cost_gbp": format(proposal.expected_entry_cost_gbp, "f"),
                "expected_exit_cost_gbp": format(proposal.expected_exit_cost_gbp, "f"),
                "adverse_allowance_gbp": format(proposal.adverse_allowance_gbp, "f"),
            },
        },
    )


def _reset_body(
    *,
    reason: str,
    target: str,
    generation_id: str,
    policy_hash: str,
    sequence: int,
    nonce: str,
    moment: object,
) -> dict[str, object]:
    from datetime import datetime

    assert isinstance(moment, datetime)
    return {
        "op": "reset",
        "account_id": "sim-primary",
        "environment": "simulated",
        "halt_reason": "DAILY_LOSS",
        "reason": reason,
        "target_london_date": target,
        "expected_generation_id": generation_id,
        "expected_policy_hash": policy_hash,
        "expected_chain_seq": sequence,
        "nonce": nonce,
        "expires_at": (moment + timedelta(hours=1)).isoformat(),
        "clock": {"as_of": moment.isoformat(), "monotonic_ns": 99},
    }


def _sequence(coordinator: Coordinator) -> int:
    status = send(coordinator.owner, {"op": "status"})
    return int(status["head"]["sequence"])


def _snapshot(store: object) -> dict[str, object]:
    from sterling.gate2.store import Gate2Store

    assert isinstance(store, Gate2Store)
    row = store.connection.execute("SELECT last_clock FROM meta").fetchone()
    count = store.connection.execute("SELECT COUNT(*) AS n FROM reservations").fetchone()
    assert row is not None and count is not None
    return {"last_clock": str(row["last_clock"]), "reservations": int(count["n"])}


def test_direct_library_call_is_not_the_ipc_boundary(tmp_path: Path) -> None:
    """The in-process API is the coordinator. The socket test above is the boundary."""
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    from tests.gate2.support import clock

    decision = admit(store, request("library"), clock(FUTURE))
    assert decision.execution_permission_issued is False
    store.close()

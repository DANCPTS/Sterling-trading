"""Checkpoint repair does not bless a stale or hostile owner anchor."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.gate2.peers import Coordinator, send
from tests.gate2.support import fresh_book, new_store

from sterling.gate2.errors import StoreBlocked
from sterling.gate2.models import HaltName
from sterling.gate2.state import active_halts
from sterling.gate2.store import open_store, write_owner_anchor


def test_reconcile_repairs_checkpoint_lag_and_keeps_halts(tmp_path: Path) -> None:
    uid = os.getuid()
    store = new_store(tmp_path, owner_uid=uid, ingress_uid=uid, risk_uid=uid, ai_uid=uid + 3)
    book = fresh_book(tmp_path / "book.jsonl")
    from tests.gate2.support import anchor

    anchor(store, book, tmp_path / "book.jsonl")
    root = store.root
    early = (root / "checkpoint.json").read_bytes()
    store.close()
    coordinator = Coordinator(root, tmp_path / "socks")
    coordinator.start()
    try:
        paused = send(coordinator.owner, {"op": "pause", "reason": "hold this latch"})
        assert "OWNER_PAUSE_LATCHED" in paused["reasons"]
        refused = send(coordinator.proposal, {"op": "reconcile"})
        assert "PROPOSAL_CHANNEL_DENIED" in refused["reasons"]
    finally:
        coordinator.shutdown()
    (root / "checkpoint.json").write_bytes(early)
    lagged = open_store(root)
    assert lagged.blocked == "CHECKPOINT_LAG"
    assert HaltName.OWNER_PAUSE.value in active_halts(lagged.connection)
    halted_rows = lagged.connection.execute("SELECT COUNT(*) AS n FROM halt_events").fetchone()
    lagged.close()
    coordinator.start()
    try:
        repaired = send(coordinator.owner, {"op": "reconcile"})
        assert repaired["execution_permission_issued"] is False
        assert "CHECKPOINT_RECONCILED" in repaired["reasons"]
    finally:
        coordinator.shutdown()
    reopened = open_store(root)
    assert reopened.blocked is None
    assert HaltName.OWNER_PAUSE.value in active_halts(reopened.connection)
    again = reopened.connection.execute("SELECT COUNT(*) AS n FROM halt_events").fetchone()
    assert halted_rows is not None and again is not None
    assert int(again["n"]) == int(halted_rows["n"])
    audit = reopened.connection.execute("SELECT kind FROM audit WHERE kind = 'CHECKPOINT_RECONCILE'").fetchone()
    assert audit is not None
    reopened.close()


def test_reconcile_cannot_clear_an_owner_anchor_lag_or_mismatch(tmp_path: Path) -> None:
    uid = os.getuid()
    store = new_store(
        tmp_path,
        owner_uid=uid,
        ingress_uid=uid,
        risk_uid=uid,
        ai_uid=uid + 4,
        require_owner_anchor=True,
    )
    root = store.root
    anchor_path = tmp_path / "owner-anchor.json"
    write_owner_anchor(anchor_path, store._read_head_checkpoint())
    store.close()
    opened = open_store(root, owner_anchor=anchor_path)
    assert opened.blocked is None
    opened.close()
    coordinator = Coordinator(root, tmp_path / "socks", owner_anchor=anchor_path)
    coordinator.start()
    try:
        paused = send(coordinator.owner, {"op": "pause", "reason": "preserve me"})
        assert "OWNER_PAUSE_LATCHED" in paused["reasons"]
        refused = send(coordinator.proposal, {"op": "confirm_anchor"})
        assert "PROPOSAL_CHANNEL_DENIED" in refused["reasons"]
    finally:
        coordinator.shutdown()
    # The pause published a new journal checkpoint. Put the owner anchor back
    # on the previous head.
    write_owner_anchor(anchor_path, _checkpoint_from(root, sequence_back=True))
    lagged = open_store(root, owner_anchor=anchor_path)
    assert lagged.blocked == "OWNER_ANCHOR_LAG"
    assert HaltName.OWNER_PAUSE.value in active_halts(lagged.connection)
    with pytest.raises(StoreBlocked, match="OWNER_ANCHOR_LAG"):
        lagged.reconcile_checkpoint(actor_uid=uid)
    lagged.close()
    restarted = open_store(root, owner_anchor=anchor_path)
    assert restarted.blocked == "OWNER_ANCHOR_LAG"
    restarted.close()
    current = open_store(root, owner_anchor=anchor_path)
    write_owner_anchor(anchor_path, current._read_head_checkpoint())
    assert current.confirm_owner_anchor(actor_uid=uid) == "REFRESH_REQUIRED"
    assert current.blocked == "OWNER_ANCHOR_LAG"
    current.close()
    still = open_store(root, owner_anchor=anchor_path)
    assert still.blocked == "OWNER_ANCHOR_LAG"
    assert HaltName.OWNER_PAUSE.value in active_halts(still.connection)
    write_owner_anchor(anchor_path, still._read_head_checkpoint())
    assert still.confirm_owner_anchor(actor_uid=uid) == "CONFIRMED"
    assert still.blocked is None
    still.close()
    final = open_store(root, owner_anchor=anchor_path)
    assert final.blocked is None
    assert HaltName.OWNER_PAUSE.value in active_halts(final.connection)
    ahead = final._read_head_checkpoint()
    from sterling.gate2.store import Checkpoint

    write_owner_anchor(
        anchor_path,
        Checkpoint(
            store_id=ahead.store_id,
            account_id=ahead.account_id,
            environment=ahead.environment,
            schema_version=ahead.schema_version,
            sequence=ahead.sequence + 5,
            chain_hash=ahead.chain_hash,
            policy_hash=ahead.policy_hash,
        ),
    )
    final.close()
    with pytest.raises(StoreBlocked, match="OWNER_ANCHOR_MISMATCH"):
        open_store(root, owner_anchor=anchor_path)


def _checkpoint_from(root: Path, *, sequence_back: bool) -> object:
    """Return the current head, or the genesis checkpoint saved beside it."""
    store = open_store(root)
    try:
        if not sequence_back:
            return store._read_head_checkpoint()
        row = store.connection.execute("SELECT chain_hash FROM chain WHERE seq = 1").fetchone()
        head = store._read_head_checkpoint()
        from sterling.gate2.store import Checkpoint

        assert row is not None
        return Checkpoint(
            store_id=head.store_id,
            account_id=head.account_id,
            environment=head.environment,
            schema_version=head.schema_version,
            sequence=1,
            chain_hash=str(row["chain_hash"]),
            policy_hash=head.policy_hash,
        )
    finally:
        store.close()


def test_missing_database_is_not_replaced(tmp_path: Path) -> None:
    with pytest.raises(StoreBlocked, match="STORE_MISSING"):
        open_store(tmp_path / "absent")
    assert not (tmp_path / "absent" / "journal.sqlite").exists()

"""Owner socket resets use SO_PEERCRED of this process. That is not a second UID."""

from __future__ import annotations

import json
import os
import socket
import threading
from datetime import timedelta
from pathlib import Path

from tests.gate2.support import MOMENT, anchor, fresh_book, new_store

from sterling.gate2.clock import FixtureClock
from sterling.gate2.models import HaltName
from sterling.gate2.service import serve
from sterling.gate2.state import active_halts
from sterling.gate2.store import open_store


def test_owner_pause_and_reset_use_the_socket_peer(tmp_path: Path) -> None:
    uid = os.getuid()
    store = new_store(tmp_path, owner_uid=uid, ai_uid=uid + 7, risk_uid=uid, ingress_uid=uid)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    root = store.root
    policy_hash = store.identity.policy_hash
    generation_id = str(store.connection.execute("SELECT generation_id FROM current_generation").fetchone()["generation_id"])
    store.close()
    sockets = tmp_path / "socks"
    stop = tmp_path / "stop"
    thread = threading.Thread(
        target=serve,
        kwargs={
            "root": root,
            "proposal_path": sockets / "proposal.sock",
            "owner_path": sockets / "owner.sock",
            "ingress_path": sockets / "ingress.sock",
            "stop_file": stop,
            "clock_source": FixtureClock(MOMENT),
        },
        daemon=True,
    )
    thread.start()
    _wait(sockets / "owner.sock")
    try:
        paused = _send(
            sockets / "owner.sock",
            {
                "op": "pause",
                "reason": "desk halt for the session",
                "clock": {"as_of": MOMENT.isoformat(), "monotonic_ns": 1},
            },
        )
        assert "OWNER_PAUSE_LATCHED" in paused["reasons"]
        refused = _send(sockets / "proposal.sock", {"op": "reset", "reason": "ai must not reset"})
        assert "PROPOSAL_CHANNEL_DENIED" in refused["reasons"]
        sql = _send(sockets / "proposal.sock", {"op": "sql", "sql": "delete from meta"})
        assert "OPERATION_DENIED" in sql["reasons"]
        assert sql["execution_permission_issued"] is False
        opened = open_store(root)
        assert "OWNER_PAUSE" in active_halts(opened.connection)
        seq, _link = opened.head()
        opened.close()
        cleared = _send(
            sockets / "owner.sock",
            {
                "op": "reset",
                "account_id": "sim-primary",
                "environment": "simulated",
                "halt_reason": "OWNER_PAUSE",
                "reason": "owner reviewed the pause",
                "target_london_date": None,
                "expected_generation_id": generation_id,
                "expected_policy_hash": policy_hash,
                "expected_chain_seq": seq,
                "nonce": "nonce-clear",
                "expires_at": (MOMENT + timedelta(hours=1)).isoformat(),
                "clock": {"as_of": MOMENT.isoformat(), "monotonic_ns": 4},
            },
        )
        assert "RESET_APPLIED" in cleared["reasons"]
        assert cleared["execution_permission_issued"] is False
        opened = open_store(root)
        assert HaltName.OWNER_PAUSE.value not in active_halts(opened.connection)
        opened.close()
        blank = _send(
            sockets / "owner.sock",
            {
                "op": "reset",
                "account_id": "sim-primary",
                "environment": "simulated",
                "halt_reason": "OWNER_PAUSE",
                "reason": "   ",
                "target_london_date": None,
                "expected_generation_id": "none",
                "expected_policy_hash": policy_hash,
                "expected_chain_seq": seq,
                "nonce": "nonce-blank",
                "expires_at": (MOMENT + timedelta(hours=1)).isoformat(),
                "clock": {"as_of": MOMENT.isoformat(), "monotonic_ns": 2},
            },
        )
        assert "RESET_REASON_MISSING" in blank["reasons"]
        stale = _send(
            sockets / "owner.sock",
            {
                "op": "reset",
                "account_id": "sim-primary",
                "environment": "simulated",
                "halt_reason": "OWNER_PAUSE",
                "reason": "stale after the clear",
                "target_london_date": None,
                "expected_generation_id": "missing",
                "expected_policy_hash": policy_hash,
                "expected_chain_seq": seq,
                "nonce": "nonce-stale",
                "expires_at": (MOMENT + timedelta(hours=1)).isoformat(),
                "clock": {"as_of": MOMENT.isoformat(), "monotonic_ns": 3},
            },
        )
        assert "RESET_APPLIED" not in stale["reasons"]
        opened = open_store(root)
        assert HaltName.OWNER_PAUSE.value not in active_halts(opened.connection)
        opened.close()
    finally:
        stop.write_text("stop", encoding="utf-8")
        thread.join(timeout=5)


def _wait(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        import time

        time.sleep(0.02)
    raise AssertionError(path)


def _send(path: Path, payload: dict[str, object]) -> dict[str, object]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(str(path))
    connection.sendall(json.dumps(payload).encode("utf-8") + b"\n")
    data = b""
    while b"\n" not in data:
        piece = connection.recv(65536)
        if not piece:
            break
        data += piece
    body = json.loads(data.decode("utf-8"))
    assert isinstance(body, dict)
    return body

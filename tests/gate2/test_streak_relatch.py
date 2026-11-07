"""A late fee on a pre-reset episode reveals three losses and re-latches.

The owner restart goes through the owner socket. The original fills stay in
the ledger. The fee is an appended correction tied to the original buy.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from tests.gate2.peers import Coordinator, send
from tests.gate2.support import MOMENT, anchor, clock, fresh_book, new_store, quote_for, request
from tests.support import settle

from sterling.gate2.admit import admit
from sterling.gate2.models import HaltName
from sterling.gate2.state import active_halts
from sterling.gate2.store import open_store
from sterling.ledger.events import FeeEvent
from sterling.ledger.ledger import Ledger


def test_pre_reset_fee_reveals_three_losses_and_relatches(tmp_path: Path) -> None:
    uid = os.getuid()
    store = new_store(tmp_path, owner_uid=uid, ingress_uid=uid, risk_uid=uid, ai_uid=uid + 9)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    assert "GENERATION_ANCHORED" in anchor(store, book, path).reasons
    # Two losses, one win, then three losses. The win keeps the first run
    # short of three. The later three latch the streak.
    for index, sell in enumerate(("9.50", "9.50", "10.50", "9.50", "9.50", "9.50"), start=1):
        _round_trip(book, index, sell)
    latched = anchor(store, book, path, 2, opening=False, quotes={"SYNTH-EQ": quote_for("10")})
    assert "GENERATION_ANCHORED" in latched.reasons
    assert HaltName.LOSS_STREAK.value in active_halts(store.connection)
    assert HaltName.DAILY_LOSS.value not in active_halts(store.connection)
    before = _net(store, "SYNTH-EQ:buy-3")
    assert before > 0
    root = store.root
    policy_hash = store.identity.policy_hash
    generation_id = str(store.connection.execute("SELECT generation_id FROM current_generation").fetchone()["generation_id"])
    store.close()

    coordinator = Coordinator(root, tmp_path / "socks")
    coordinator.start()
    try:
        head = send(coordinator.owner, {"op": "status", "clock": _clock()})
        applied = send(
            coordinator.owner,
            {
                "op": "reset",
                "account_id": "sim-primary",
                "environment": "simulated",
                "halt_reason": "LOSS_STREAK",
                "reason": "owner acknowledges the recorded streak and starts a new epoch",
                "target_london_date": None,
                "expected_generation_id": generation_id,
                "expected_policy_hash": policy_hash,
                "expected_chain_seq": int(head["head"]["sequence"]),
                "nonce": "streak-restart",
                "expires_at": (MOMENT + timedelta(hours=2)).isoformat(),
                "clock": _clock(),
            },
        )
        assert applied["reasons"] == ["RESET_APPLIED"]
        assert applied["execution_permission_issued"] is False
    finally:
        coordinator.shutdown()

    restarted = open_store(root)
    assert HaltName.LOSS_STREAK.value not in active_halts(restarted.connection)
    epoch = restarted.connection.execute("SELECT acknowledged_json, active FROM streak_epochs").fetchone()
    assert epoch is not None and int(epoch["active"]) == 1
    acknowledged = json.loads(str(epoch["acknowledged_json"]))
    stored_win = next(item for item in acknowledged if item["episode_id"] == "SYNTH-EQ:buy-3")
    assert Decimal(str(stored_win["net_gbp"])) > 0
    audit = restarted.connection.execute("SELECT actor_uid, reason, outcome FROM audit WHERE kind = 'RESET'").fetchone()
    assert audit is not None
    assert int(audit["actor_uid"]) == uid
    assert audit["outcome"] == "RESET_APPLIED"
    restarted.close()

    corrected = Ledger.load(path)
    corrected.post(
        FeeEvent(
            "late-on-buy-3",
            MOMENT,
            "commission",
            Decimal("1.00"),
            "GBP",
            Decimal("1"),
            related_event_id="buy-3",
        )
    )
    store = open_store(root)
    ingested = anchor(store, corrected, path, 3, opening=False, quotes={"SYNTH-EQ": quote_for("10")})
    assert "GENERATION_ANCHORED" in ingested.reasons
    assert HaltName.LOSS_STREAK.value in active_halts(store.connection)
    evidence = store.connection.execute(
        """
        SELECT evidence FROM halt_events
        WHERE reason = 'LOSS_STREAK' AND action = 'TRIGGER'
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    assert evidence is not None
    assert json.loads(str(evidence["evidence"]))["why"] == "pre_anchor_correction"
    assert _net(store, "SYNTH-EQ:buy-3") < 0
    denied = admit(store, request("after-correction"), clock())
    assert HaltName.LOSS_STREAK.value in denied.reasons
    assert denied.reservation_id is None
    assert denied.execution_permission_issued is False
    store.close()

    durable = open_store(root)
    assert HaltName.LOSS_STREAK.value in active_halts(durable.connection)
    epoch_after = durable.connection.execute("SELECT acknowledged_json FROM streak_epochs WHERE active = 1").fetchone()
    assert epoch_after is not None
    still_original = json.loads(str(epoch_after["acknowledged_json"]))
    remembered = next(item for item in still_original if item["episode_id"] == "SYNTH-EQ:buy-3")
    assert Decimal(str(remembered["net_gbp"])) == Decimal(str(stored_win["net_gbp"]))
    assert _net(durable, "SYNTH-EQ:buy-3") < 0
    loaded = Ledger.load(path)
    ids = [event.event_id for event in loaded.events]
    assert "buy-3" in ids
    assert "sell-3" in ids
    assert "late-on-buy-3" in ids
    assert ids.index("buy-3") < ids.index("late-on-buy-3")
    durable.close()


def _round_trip(book: Ledger, index: int, sell: str) -> None:
    settle(book, event_id=f"buy-{index}", timestamp=MOMENT, instrument_id="SYNTH-EQ", side="buy", quantity="1", price="10")
    settle(
        book,
        event_id=f"sell-{index}",
        timestamp=MOMENT,
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="1",
        price=sell,
    )


def _net(store: object, episode_id: str) -> Decimal:
    from sterling.gate2.store import Gate2Store

    assert isinstance(store, Gate2Store)
    row = store.connection.execute(
        "SELECT net_gbp FROM episode_cache WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    assert row is not None
    return Decimal(str(row["net_gbp"]))


def _clock() -> dict[str, object]:
    return {"as_of": MOMENT.isoformat(), "monotonic_ns": 1}

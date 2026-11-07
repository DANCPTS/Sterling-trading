"""Daily-loss reset through the owner socket, then a real reopen.

The other halt stays. A same-day, empty, unverified, or baselineless request
does not clear the latch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tests.gate2.peers import Coordinator, send
from tests.gate2.support import MOMENT, anchor, buy_marked, clock, fresh_book, new_store, quote_for, request
from tests.support import june

from sterling.gate2.admit import admit
from sterling.gate2.models import HaltName, StopEvidence
from sterling.gate2.state import active_halts
from sterling.gate2.store import open_store
from sterling.risk.reasons import Reason

DAY2 = june(2, 10)
DAY2_LATER = june(2, 11)
STOP = StopEvidence(stop_price=Decimal("90"), covered_quantity=Decimal("1"))


def test_daily_loss_resets_only_on_a_later_verified_day(tmp_path: Path) -> None:
    uid = os.getuid()
    store = new_store(tmp_path, owner_uid=uid, ingress_uid=uid, risk_uid=uid, ai_uid=uid + 9)
    path = tmp_path / "book.jsonl"
    book = fresh_book(path)
    opened = anchor(store, book, path, 1, moment=june(1, 9), reported_equity="500.00")
    assert "GENERATION_ANCHORED" in opened.reasons
    buy_marked(book, instrument="SYNTH-EQ", price="100", mark="95", moment=MOMENT)
    loss = anchor(
        store,
        book,
        path,
        2,
        moment=MOMENT,
        opening=False,
        quotes={"SYNTH-EQ": quote_for("95", MOMENT)},
        stops={"SYNTH-EQ": STOP},
    )
    assert "GENERATION_ANCHORED" in loss.reasons
    assert HaltName.DAILY_LOSS.value in active_halts(store.connection)
    root = store.root
    policy_hash = store.identity.policy_hash
    store.close()

    coordinator = Coordinator(root, tmp_path / "socks")
    coordinator.start()
    try:
        paused = send(
            coordinator.owner,
            {"op": "pause", "reason": "keep this halt", "clock": _clock(MOMENT)},
        )
        assert "OWNER_PAUSE_LATCHED" in paused["reasons"]
        head = send(coordinator.owner, {"op": "status", "clock": _clock(MOMENT)})
        sequence = int(head["head"]["sequence"])
        generation_id = _current_generation(root)
        same_day = _reset(
            coordinator.owner,
            reason="same calendar day must not clear it",
            target="2026-06-01",
            generation_id=generation_id,
            policy_hash=policy_hash,
            sequence=sequence,
            nonce="daily-same-day",
            moment=MOMENT,
        )
        assert "RESET_APPLIED" not in same_day["reasons"]
        assert Reason.RESET_DAILY_REQUIRES_TRADING_DAY_TRANSITION.value in same_day["reasons"]
        blank = _reset(
            coordinator.owner,
            reason="   ",
            target="2026-06-02",
            generation_id=generation_id,
            policy_hash=policy_hash,
            sequence=sequence,
            nonce="daily-blank",
            moment=MOMENT,
        )
        assert Reason.RESET_REASON_MISSING.value in blank["reasons"]
        forged = send(coordinator.proposal, {"op": "reset", "reason": "proposal channel is not the owner"})
        assert "PROPOSAL_CHANNEL_DENIED" in forged["reasons"]
        _stranger_cannot_open_owner_socket(coordinator.owner)
    finally:
        coordinator.shutdown()

    reopened = open_store(root)
    assert HaltName.DAILY_LOSS.value in active_halts(reopened.connection)
    assert HaltName.OWNER_PAUSE.value in active_halts(reopened.connection)
    no_baseline = anchor(
        reopened,
        book,
        path,
        3,
        moment=DAY2,
        opening=False,
        verified=False,
        quotes={"SYNTH-EQ": quote_for("95", DAY2)},
        stops={"SYNTH-EQ": STOP},
    )
    assert "GENERATION_ANCHORED" in no_baseline.reasons
    reopened.close()
    coordinator.start()
    try:
        coordinator.clock.set(DAY2)
        head = send(coordinator.owner, {"op": "status", "clock": _clock(DAY2)})
        unverified = _reset(
            coordinator.owner,
            reason="day changed but the session is not verified and there is no opening baseline",
            target="2026-06-02",
            generation_id=_current_generation(root),
            policy_hash=policy_hash,
            sequence=int(head["head"]["sequence"]),
            nonce="daily-unverified",
            moment=DAY2,
        )
        assert "RESET_APPLIED" not in unverified["reasons"]
        assert Reason.SESSION_NOT_CONFIRMED.value in unverified["reasons"]
    finally:
        coordinator.shutdown()

    still = open_store(root)
    assert HaltName.DAILY_LOSS.value in active_halts(still.connection)
    opening = anchor(
        still,
        book,
        path,
        4,
        moment=DAY2,
        opening=True,
        verified=True,
        quotes={"SYNTH-EQ": quote_for("95", DAY2)},
        stops={"SYNTH-EQ": STOP},
    )
    assert "GENERATION_ANCHORED" in opening.reasons
    current = anchor(
        still,
        book,
        path,
        5,
        moment=DAY2_LATER,
        opening=False,
        verified=True,
        quotes={"SYNTH-EQ": quote_for("95", DAY2_LATER)},
        stops={"SYNTH-EQ": STOP},
    )
    assert "GENERATION_ANCHORED" in current.reasons
    assert HaltName.DAILY_LOSS.value in active_halts(still.connection)
    assert HaltName.OWNER_PAUSE.value in active_halts(still.connection)
    current_id = str(still.connection.execute("SELECT generation_id FROM current_generation").fetchone()["generation_id"])
    baseline = still.connection.execute("SELECT equity_gbp FROM baselines WHERE london_date = '2026-06-02'").fetchone()
    assert baseline is not None
    still.close()

    coordinator.start()
    try:
        coordinator.clock.set(DAY2_LATER)
        head = send(coordinator.owner, {"op": "status", "clock": _clock(DAY2_LATER)})
        applied = _reset(
            coordinator.owner,
            reason="later verified London day with a fresh opening and current book",
            target="2026-06-02",
            generation_id=current_id,
            policy_hash=policy_hash,
            sequence=int(head["head"]["sequence"]),
            nonce="daily-valid",
            moment=DAY2_LATER,
        )
        assert applied["reasons"] == ["RESET_APPLIED"]
        assert applied["execution_permission_issued"] is False
        assert applied["ticket_issued"] is False
    finally:
        coordinator.shutdown()

    durable = open_store(root)
    halts = active_halts(durable.connection)
    assert HaltName.DAILY_LOSS.value not in halts
    assert HaltName.OWNER_PAUSE.value in halts
    audit = durable.connection.execute(
        "SELECT actor_uid, reason, outcome FROM audit WHERE kind = 'RESET' AND outcome = 'RESET_APPLIED'"
    ).fetchone()
    assert audit is not None
    assert int(audit["actor_uid"]) == uid
    assert "fresh opening" in str(audit["reason"])
    denied = admit(durable, request("after-daily-reset"), clock(DAY2_LATER, monotonic_ns=4))
    assert denied.execution_permission_issued is False
    assert HaltName.OWNER_PAUSE.value in denied.reasons
    assert denied.reservation_id is None
    durable.close()

    coordinator.clock.set(DAY2_LATER)
    coordinator.start()
    try:
        replayed = _reset(
            coordinator.owner,
            reason="later verified London day with a fresh opening and current book",
            target="2026-06-02",
            generation_id=current_id,
            policy_hash=policy_hash,
            sequence=0,
            nonce="daily-valid",
            moment=DAY2_LATER,
        )
        assert replayed["replayed"] is True
        assert "RESET_APPLIED" in replayed["reasons"]
    finally:
        coordinator.shutdown()
    final = open_store(root)
    assert HaltName.DAILY_LOSS.value not in active_halts(final.connection)
    assert HaltName.OWNER_PAUSE.value in active_halts(final.connection)
    final.close()


def _reset(
    path: Path,
    *,
    reason: str,
    target: str,
    generation_id: str,
    policy_hash: str,
    sequence: int,
    nonce: str,
    moment: datetime,
) -> dict[str, object]:
    body = send(
        path,
        {
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
            "clock": _clock(moment),
        },
    )
    return body


def _clock(moment: datetime) -> dict[str, object]:
    return {"as_of": moment.isoformat(), "monotonic_ns": 1}


def _current_generation(root: Path) -> str:
    store = open_store(root)
    try:
        row = store.connection.execute("SELECT generation_id FROM current_generation").fetchone()
        assert row is not None and row["generation_id"] is not None
        return str(row["generation_id"])
    finally:
        store.close()


def _stranger_cannot_open_owner_socket(path: Path) -> None:
    """A different uid must not clear the latch. Root can create that uid."""
    if os.geteuid() != 0:
        return
    name = "sterling-g2-stranger"
    import pwd

    try:
        pwd.getpwnam(name)
    except KeyError:
        created = subprocess.run(["useradd", "--system", "--create-home", "--user-group", name], check=False)
        if created.returncode != 0:
            raise AssertionError("BLOCKED: cannot create a non-owner uid for the reset denial") from None
    probe = subprocess.run(
        [
            "runuser",
            "-u",
            name,
            "--",
            sys.executable,
            "-c",
            (
                "import json,socket,sys\n"
                "s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                "try:\n"
                f" s.connect({str(path)!r})\n"
                " print(json.dumps({'denied': False}))\n"
                "except OSError as exc:\n"
                " print(json.dumps({'denied': True, 'errno': exc.errno}))\n"
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    body = json.loads(probe.stdout)
    assert body["denied"] is True

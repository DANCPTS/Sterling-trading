"""Process-killing faults around the commit boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.gate2.support import anchor, fresh_book, new_store
from tests.gate2.test_concurrency import _payload

from sterling.gate2.errors import StoreBlocked
from sterling.gate2.store import open_store


def _run(root: Path, fault: str, kind: str = "crash") -> subprocess.CompletedProcess[str]:
    path = root / "request.json"
    path.write_text(json.dumps(_payload("crash")), encoding="utf-8")
    from tests.gate2.support import MOMENT

    from sterling.gate2.clock import FixtureClock

    FixtureClock(MOMENT).save(root / "coordinator-clock.json")
    env = os.environ.copy()
    env["STERLING_GATE2_FAULT"] = fault
    env["STERLING_GATE2_FAULT_KIND"] = kind
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "sterling.gate2.worker",
            "admit",
            "--root",
            str(root / "journal"),
            "--request",
            str(path),
            "--clock-file",
            str(root / "coordinator-clock.json"),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_crash_before_commit_leaves_no_hold(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    store.close()
    completed = _run(tmp_path, "before_commit")
    assert completed.returncode == 86
    reopened = open_store(tmp_path / "journal")
    count = reopened.connection.execute("SELECT COUNT(*) AS n FROM reservations").fetchone()
    assert count is not None and int(count["n"]) == 0
    assert reopened.blocked is None
    reopened.close()


def test_crash_after_commit_before_reply_keeps_one_hold_and_blocks_on_checkpoint(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    store.close()
    completed = _run(tmp_path, "after_commit_before_reply")
    assert completed.returncode == 86
    reopened = open_store(tmp_path / "journal")
    assert reopened.blocked == "CHECKPOINT_LAG"
    count = reopened.connection.execute("SELECT COUNT(*) AS n FROM reservations WHERE state = 'RESERVED'").fetchone()
    assert count is not None and int(count["n"]) == 1
    reopened.reconcile_checkpoint()
    assert reopened.blocked is None
    # Same request id replays the committed decision and does not add a hold.
    replay = _run_clean(tmp_path)
    assert replay["replayed"] is True
    assert replay["reservation_state"] == "RESERVED"
    count_after = reopened.connection.execute("SELECT COUNT(*) AS n FROM reservations").fetchone()
    assert count_after is not None and int(count_after["n"]) == 1
    reopened.close()


def test_injected_io_in_process_returns_no_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_GATE2_FAULT", "before_commit")
    monkeypatch.setenv("STERLING_GATE2_FAULT_KIND", "io")
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    from tests.gate2.support import clock, request

    from sterling.gate2.admit import admit
    from sterling.gate2.models import Outcome

    decision = admit(store, request("io"), clock())
    assert decision.outcome == Outcome.UNAVAILABLE
    assert decision.reservation_id is None
    count = store.connection.execute("SELECT COUNT(*) AS n FROM reservations").fetchone()
    assert count is not None and int(count["n"]) == 0


def test_injected_io_before_commit_returns_no_success(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    book = fresh_book(tmp_path / "book.jsonl")
    anchor(store, book, tmp_path / "book.jsonl")
    store.close()
    completed = _run(tmp_path, "before_commit", kind="io")
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["outcome"] == "UNAVAILABLE"
    assert payload["execution_permission_issued"] is False
    reopened = open_store(tmp_path / "journal")
    count = reopened.connection.execute("SELECT COUNT(*) AS n FROM reservations").fetchone()
    assert count is not None and int(count["n"]) == 0
    reopened.close()


def _run_clean(root: Path) -> dict[str, object]:
    path = root / "request.json"
    env = os.environ.copy()
    env.pop("STERLING_GATE2_FAULT", None)
    env.pop("STERLING_GATE2_FAULT_KIND", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sterling.gate2.worker",
            "admit",
            "--root",
            str(root / "journal"),
            "--request",
            str(path),
            "--clock-file",
            str(root / "coordinator-clock.json"),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    body = json.loads(completed.stdout)
    assert isinstance(body, dict)
    return body


def test_open_after_truncated_database_does_not_invent_ready(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    root = store.root
    store.close()
    (root / "journal.sqlite").write_bytes(b"")
    try:
        open_store(root)
        raised = False
    except StoreBlocked:
        raised = True
    assert raised

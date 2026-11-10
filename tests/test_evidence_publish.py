"""The evidence branch keeps every record. No force-push and no second writer."""

from __future__ import annotations

import fcntl
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.verify import verify_modes

PUBLISH = Path(__file__).resolve().parents[1] / "deploy" / "private-runner" / "publish_evidence.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True)


def _bare(tmp: Path) -> Path:
    bare = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True)
    seed = tmp / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "evidence")
    _git(seed, "config", "user.email", "sterling@example.com")
    _git(seed, "config", "user.name", "Sterling")
    log = seed / "evidence" / "forward.jsonl"
    log.parent.mkdir()
    ProposalLog(log).create(now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC))
    _git(seed, "add", "evidence/forward.jsonl")
    _git(seed, "commit", "-m", "Start the evidence log")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "evidence")
    return bare


def _clone(bare: Path, dest: Path) -> None:
    subprocess.run(["git", "clone", "--branch", "evidence", str(bare), str(dest)], check=True)
    _git(dest, "config", "user.email", "sterling@example.com")
    _git(dest, "config", "user.name", "Sterling")


def _append(clone: Path, week: str, when: datetime) -> None:
    log = ProposalLog(clone / "evidence" / "forward.jsonl")
    log.append_event(
        timestamp_utc=when,
        record_type="missed_week",
        fields={
            "clock_source": "system",
            "detected_at": when.isoformat(),
            "mode": "forward",
            "note": "MISSED week. Not a backfill and not a trade.",
            "order": "detection",
            "recorded_at": when.isoformat(),
            "week_key": week,
        },
    )


def test_two_runs_a_lock_and_a_conflict_keep_every_record(tmp_path: Path) -> None:
    bare = _bare(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    _clone(bare, first)
    _clone(bare, second)
    _append(first, "2026-W45", datetime(2026, 11, 2, 16, 0, tzinfo=UTC))
    _append(second, "2026-W46", datetime(2026, 11, 9, 16, 0, tzinfo=UTC))
    assert _publish(first) == "pushed"
    assert _publish(second) == "pushed"
    checked = tmp_path / "checked"
    _clone(bare, checked)
    log = ProposalLog(checked / "evidence" / "forward.jsonl")
    assert log.verify() == 3
    weeks = [record.get("week_key") for record in log.records() if record.get("record_type") == "missed_week"]
    assert weeks == ["2026-W45", "2026-W46"]
    verify_modes(log.records(), now=datetime.now(UTC))

    lock = (first / ".evidence.lock").open("a", encoding="utf-8")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    try:
        raced = subprocess.run(
            [sys.executable, str(PUBLISH), "--clone", str(first)],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    assert raced.returncode == 0
    assert "skipped_locked" in raced.stdout
    again = tmp_path / "after-lock"
    _clone(bare, again)
    assert ProposalLog(again / "evidence" / "forward.jsonl").verify() == 3


def _publish(clone: Path) -> str:
    completed = subprocess.run(
        [sys.executable, str(PUBLISH), "--clone", str(clone)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_publish_refuses_a_branch_other_than_evidence(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("publish_evidence", PUBLISH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    try:
        loaded.publish(tmp_path, branch="main")
    except RuntimeError as exc:
        assert "evidence" in str(exc)
    else:
        raise AssertionError("main was accepted")

"""Append the forward log to the private repo's evidence branch.

``main`` stays protected. This script refuses every other branch name. It
pushes fast-forward only. If the remote evidence branch moved, records that
are only in the local log are re-appended onto that chain. A remote line is
never deleted and the push is never forced.

The workflow's concurrency group allows one job. This lock also stops a second
process in the same clone.
"""

from __future__ import annotations

import argparse
import fcntl
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from sterling.research.fx.grok_forward import ProposalLog

LOG_NAME = "evidence/forward.jsonl"


def publish(clone: Path, *, branch: str = "evidence", remote: str = "origin") -> str:
    if branch != "evidence":
        raise RuntimeError("refusing to publish outside the evidence branch")
    lock_path = clone / ".evidence.lock"
    lock_path.touch()
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "skipped_locked"
        _git(clone, "fetch", remote, check=False)
        local = clone / LOG_NAME
        if not local.is_file():
            raise RuntimeError("local evidence log is missing")
        remote_text = _remote_log(clone, remote, branch)
        merged = _merge(remote_text, local.read_text(encoding="utf-8"), clone / "evidence")
        if _remote_exists(clone, remote, branch):
            _git(clone, "checkout", "-f", "-B", branch, f"{remote}/{branch}")
        else:
            _git(clone, "checkout", "-B", branch)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(merged, encoding="utf-8")
        ProposalLog(local).verify()
        _git(clone, "add", LOG_NAME)
        if (clone / "evidence" / "first_observed.json").is_file():
            _git(clone, "add", "evidence/first_observed.json")
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=clone, check=False).returncode == 0:
            return "unchanged"
        _git(clone, "commit", "-m", "Record the weekly forward log")
        if _remote_exists(clone, remote, branch):
            _git(clone, "merge-base", "--is-ancestor", f"{remote}/{branch}", "HEAD")
        # No --force. A non-fast-forward update is rejected by git.
        _git(clone, "push", remote, f"HEAD:{branch}")
        return "pushed"


def _merge(remote_text: str, local_text: str, folder: Path) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    if not remote_text:
        return local_text
    if local_text.startswith(remote_text) or remote_text.startswith(local_text):
        return local_text if len(local_text) >= len(remote_text) else remote_text
    remote_path = folder / "remote.jsonl"
    local_path = folder / "local.jsonl"
    remote_log = ProposalLog(remote_path)
    remote_path.write_text(remote_text, encoding="utf-8")
    local_path.write_text(local_text, encoding="utf-8")
    local_log = ProposalLog(local_path)
    seen = {str(record.get("record_hash")) for record in remote_log.records()}
    for record in local_log.records():
        if str(record.get("record_hash")) in seen:
            continue
        moment = datetime.fromisoformat(str(record["timestamp_utc"]))
        tip = datetime.fromisoformat(str(remote_log.records()[-1]["timestamp_utc"]))
        if moment <= tip:
            moment = tip + timedelta(microseconds=1)
        fields = {
            key: value
            for key, value in record.items()
            if key not in {"schema", "record_type", "timestamp_utc", "prev_hash", "record_hash"}
        }
        remote_log.append_event(timestamp_utc=moment, record_type=str(record["record_type"]), fields=fields)
        seen.add(str(remote_log.records()[-1].get("record_hash")))
    return remote_path.read_text(encoding="utf-8")


def _remote_log(clone: Path, remote: str, branch: str) -> str:
    show = subprocess.run(
        ["git", "show", f"{remote}/{branch}:{LOG_NAME}"],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
    )
    if show.returncode != 0:
        return ""
    return show.stdout


def _remote_exists(clone: Path, remote: str, branch: str) -> bool:
    show = subprocess.run(["git", "rev-parse", "--verify", f"{remote}/{branch}"], cwd=clone, check=False, capture_output=True)
    return show.returncode == 0


def _git(clone: Path, *args: str, check: bool = True) -> None:
    completed = subprocess.run(["git", *args], cwd=clone, check=False, capture_output=True, text=True)
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git failed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast-forward the evidence branch. No force-push.")
    parser.add_argument("--clone", type=Path, default=Path())
    parser.add_argument("--branch", default="evidence")
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args()
    print(publish(args.clone, branch=args.branch, remote=args.remote))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

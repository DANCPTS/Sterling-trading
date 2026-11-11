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
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.verify import verify_evidence_lane
from sterling.research.invest.verify import verify_invest, verify_v1_closed

LOGS = {
    "evidence/forward.jsonl": "forward",
    "evidence/mock/forward.jsonl": "mock",
    "evidence/invest-v2/forward.jsonl": "forward",
    "evidence/invest-v2/mock/forward.jsonl": "mock",
}
# Command-local identity. Never git config --global.
_IDENTITY = (
    "-c",
    "user.name=sterling-forward-runner",
    "-c",
    "user.email=sterling-forward-runner@users.noreply.github.com",
)


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
        caller = _stdout(clone, "rev-parse", "--abbrev-ref", "HEAD")
        caller_sha = _stdout(clone, "rev-parse", "HEAD")
        _git(clone, "fetch", remote, branch, check=False)
        if not _remote_exists(clone, remote, branch):
            raise RuntimeError("evidence branch does not exist on the remote")
        present = [name for name in LOGS if (clone / name).is_file()]
        if not present:
            raise RuntimeError("local evidence log is missing")
        merged = {
            name: _merge(
                _remote_log(clone, remote, branch, name),
                (clone / name).read_text(encoding="utf-8"),
                clone / "evidence" / name.replace("/", "-"),
            )
            for name in present
        }
        states = {
            name: (clone / name).with_suffix(".state.json").read_text(encoding="utf-8")
            for name in present
            if (clone / name).with_suffix(".state.json").is_file()
        }
        observed_path = clone / "evidence" / "first_observed.json"
        observed_text = observed_path.read_text(encoding="utf-8") if observed_path.is_file() else None
        tip = _stdout(clone, "rev-parse", f"{remote}/{branch}")
        work = Path(tempfile.mkdtemp(prefix="sterling-evidence-"))
        work.rmdir()
        _git(clone, "worktree", "add", "--detach", str(work), tip)
        try:
            for name, text in merged.items():
                local = work / name
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_text(text, encoding="utf-8")
                log = ProposalLog(local)
                log.verify()
                verify_evidence_lane(log.records(), lane=LOGS[name])
                if "invest-v2" in name:
                    verify_invest(log.records(), lane=LOGS[name])
                else:
                    verify_v1_closed(log.records())
                _git(work, "add", name)
                if name in states:
                    state = local.with_suffix(".state.json")
                    state.write_text(states[name], encoding="utf-8")
                    _git(work, "add", str(state.relative_to(work)))
            if observed_text is not None:
                dest = work / "evidence" / "first_observed.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(observed_text, encoding="utf-8")
                _git(work, "add", "evidence/first_observed.json")
            quiet = subprocess.run(["git", *_IDENTITY, "diff", "--cached", "--quiet"], cwd=work, check=False)
            if quiet.returncode == 0:
                return "unchanged"
            _git(work, "commit", "-m", "Record the weekly forward log")
            _git(work, "merge-base", "--is-ancestor", tip, "HEAD")
            # No --force. A non-fast-forward update is rejected by git.
            _git(work, "push", remote, f"HEAD:{branch}")
            return "pushed"
        finally:
            _git(clone, "worktree", "remove", "--force", str(work), check=False)
            if _stdout(clone, "rev-parse", "--abbrev-ref", "HEAD") != caller:
                raise RuntimeError("publisher switched the caller checkout")
            if _stdout(clone, "rev-parse", "HEAD") != caller_sha:
                raise RuntimeError("publisher moved the caller checkout")


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


def _remote_log(clone: Path, remote: str, branch: str, name: str) -> str:
    show = subprocess.run(
        ["git", "show", f"{remote}/{branch}:{name}"],
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


def _stdout(clone: Path, *args: str) -> str:
    completed = subprocess.run(["git", *_IDENTITY, *args], cwd=clone, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git failed")
    return completed.stdout.strip()


def _git(clone: Path, *args: str, check: bool = True) -> None:
    completed = subprocess.run(["git", *_IDENTITY, *args], cwd=clone, check=False, capture_output=True, text=True)
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

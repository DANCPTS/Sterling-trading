"""One private-repo run. The evidence branch and the pinned app stay separate.

Schedule always selects xAI. A manual dispatch uses mock unless xai is chosen.
A missing key or an unknown provider fails. The success ping is not sent here:
the workflow pings only after this process exits 0, which is after the verifier
and the fast-forward push.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_MOCK_LOGS = (
    "evidence/invest-v2/mock/forward.jsonl",
    "evidence/mock/forward.jsonl",
)
_FORWARD_LOGS = (
    "evidence/invest-v2/forward.jsonl",
    "evidence/forward.jsonl",
)


def assert_named_lane(log_name: str, lane: str) -> None:
    """Mock cannot write a genuine log, and forward cannot write a mock log."""
    if lane == "mock" and log_name not in _MOCK_LOGS:
        raise RuntimeError("mock lane cannot write a genuine evidence log")
    if lane == "forward" and log_name not in _FORWARD_LOGS:
        raise RuntimeError("forward lane cannot write the mock evidence log")
    if lane not in {"mock", "forward"}:
        raise RuntimeError("lane must be mock or forward")


def select_provider(event: str, choice: str) -> str:
    if event == "schedule":
        return "xai"
    if event == "workflow_dispatch":
        if choice in {"", "mock"}:
            return "mock"
        if choice == "xai":
            return "xai"
        raise RuntimeError("provider must be mock or xai")
    raise RuntimeError("unsupported workflow event")


def require_xai_key(provider: str) -> None:
    if provider == "xai" and not os.environ.get("XAI_API_KEY", "").strip():
        raise RuntimeError("XAI_API_KEY is missing")


def read_pin(path: Path) -> str:
    pin = path.read_text(encoding="utf-8").strip().lower()
    if len(pin) != 40 or any(character not in "0123456789abcdef" for character in pin):
        raise RuntimeError("pinned app SHA is not a commit")
    return pin


def app_head(app: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(app), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("pinned app checkout has no commit")
    return completed.stdout.strip().lower()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one pinned weekly decision into the evidence branch.")
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--provider", default="")
    parser.add_argument("--pin-file", type=Path, required=True)
    parser.add_argument("--as-of", default="")
    parser.add_argument("--policy", default="grok-forward-v1")
    parser.add_argument("--allow-offline-xai", action="store_true")
    args = parser.parse_args(argv)
    pin = read_pin(args.pin_file)
    head = app_head(args.app)
    if head != pin:
        raise RuntimeError("checked out app does not match the pinned SHA")
    provider = select_provider(args.event, args.provider)
    require_xai_key(provider)
    lane = "mock" if provider == "mock" else "forward"
    if args.policy not in {"grok-forward-v1", "paper-v2"}:
        raise RuntimeError("policy must be grok-forward-v1 or paper-v2")
    if args.policy == "paper-v2":
        log_name = "evidence/invest-v2/mock/forward.jsonl" if lane == "mock" else "evidence/invest-v2/forward.jsonl"
    else:
        log_name = "evidence/mock/forward.jsonl" if lane == "mock" else "evidence/forward.jsonl"
    assert_named_lane(log_name, lane)
    _materialize(args.evidence, log_name)
    env = os.environ.copy()
    env["STERLING_APP_SHA"] = pin
    env["STERLING_EVIDENCE_LANE"] = lane
    if args.policy == "paper-v2":
        env["STERLING_SYSTEM"] = "sterling-invest"
        env["STERLING_EXPERIMENT"] = "paper-v2"
    if provider == "xai":
        env["STERLING_PUBLISH_PY"] = str(ROOT / "publish_evidence.py")
        env["STERLING_EVIDENCE_CLONE"] = str(args.evidence)
    if not args.allow_offline_xai:
        env.pop("STERLING_XAI_OFFLINE", None)
    command = [
        sys.executable,
        str(args.app / "scripts" / ("run_invest_v2.py" if args.policy == "paper-v2" else "run_weekly_decision.py")),
        "--provider",
        provider,
        "--log",
        str(args.evidence / log_name),
        "--cache",
        str(args.evidence / "evidence"),
    ]
    if provider == "xai":
        command.append("--enable-xai")
    if args.as_of:
        command.extend(["--fixture", "--simulated-clock", "--as-of", args.as_of])
    elif provider == "mock" and args.policy == "paper-v2":
        # Mock paper weeks do not fetch. Live prices belong to the xai lane.
        command.append("--fixture")
    else:
        command.append("--fetch-free")
    # The private checkout does not install sterling. Every child must see the pinned app.
    child = {**env, "PYTHONPATH": str(args.app / "src")}
    completed = subprocess.run(command, check=False, env=child)
    if completed.returncode != 0:
        raise RuntimeError("weekly decision failed")
    verify = subprocess.run(
        [sys.executable, str(ROOT / "verify_from_log.py"), "--log", str(args.evidence / log_name)],
        check=False,
        env=child,
    )
    if verify.returncode != 0:
        raise RuntimeError("verifier failed")
    publish = subprocess.run(
        [
            sys.executable,
            str(ROOT / "publish_evidence.py"),
            "--clone",
            str(args.evidence),
            "--branch",
            "evidence",
            "--remote",
            "origin",
        ],
        check=False,
        env=child,
    )
    if publish.returncode != 0:
        raise RuntimeError("evidence push failed")
    print(f"provider={provider} app_sha={pin}")
    return 0


def _materialize(clone: Path, name: str) -> None:
    """Copy the published evidence log into a fresh runner without moving main."""
    dest = clone / name
    if dest.is_file():
        return
    subprocess.run(["git", "-C", str(clone), "fetch", "origin", "evidence"], check=False, capture_output=True)
    show = subprocess.run(
        ["git", "-C", str(clone), "show", f"origin/evidence:{name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if show.returncode != 0 or not show.stdout:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(show.stdout, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

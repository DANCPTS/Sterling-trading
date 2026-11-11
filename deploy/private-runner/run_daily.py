"""Daily paper settlement. No provider, no xAI key, no schedule."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from run_private import _materialize, app_head, read_pin

ROOT = Path(__file__).resolve().parent


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mark one paper-v2 day. Does not call a model.")
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--pin-file", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--lane", choices=("mock", "forward"), default="forward")
    parsed = parser.parse_args(argv)
    pin = read_pin(parsed.pin_file)
    if app_head(parsed.app) != pin:
        raise RuntimeError("checked out app does not match the pinned SHA")
    log_name = "evidence/invest-v2/mock/forward.jsonl" if parsed.lane == "mock" else "evidence/invest-v2/forward.jsonl"
    _materialize(parsed.evidence, log_name)
    env = os.environ.copy()
    env["STERLING_APP_SHA"] = pin
    env["STERLING_EVIDENCE_LANE"] = parsed.lane
    env["STERLING_SYSTEM"] = "sterling-invest"
    env["STERLING_EXPERIMENT"] = "paper-v2"
    env.pop("XAI_API_KEY", None)
    child = {**env, "PYTHONPATH": str(parsed.app / "src")}
    command = [
        sys.executable,
        str(parsed.app / "scripts" / "run_invest_daily.py"),
        "--log",
        str(parsed.evidence / log_name),
        "--session",
        parsed.session,
        "--label",
        "live",
    ]
    completed = subprocess.run(command, check=False, env=child)
    if completed.returncode != 0:
        raise RuntimeError("daily settlement failed")
    verify = subprocess.run(
        [sys.executable, str(ROOT / "verify_from_log.py"), "--log", str(parsed.evidence / log_name)],
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
            str(parsed.evidence),
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
    print(f"daily provider_calls=0 app_sha={pin}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

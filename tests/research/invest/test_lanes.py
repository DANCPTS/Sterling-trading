"""A mock run cannot append a genuine evidence log."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sterling.research.invest.lanes import assert_lane

ROOT = Path(__file__).resolve().parents[3]


def test_mock_lane_rejects_genuine_logs(tmp_path: Path) -> None:
    genuine = (
        tmp_path / "evidence" / "forward.jsonl",
        tmp_path / "evidence" / "invest-v2" / "forward.jsonl",
    )
    mock = (
        tmp_path / "evidence" / "mock" / "forward.jsonl",
        tmp_path / "evidence" / "invest-v2" / "mock" / "forward.jsonl",
    )
    for path in genuine:
        with pytest.raises(RuntimeError, match="genuine"):
            assert_lane(path, "mock")
    for path in mock:
        assert_lane(path, "mock")
        with pytest.raises(RuntimeError, match="mock evidence"):
            assert_lane(path, "forward")
    guard = _guard()
    assert guard.assert_named_lane("evidence/invest-v2/mock/forward.jsonl", "mock") is None
    with pytest.raises(RuntimeError, match="genuine"):
        guard.assert_named_lane("evidence/invest-v2/forward.jsonl", "mock")
    with pytest.raises(RuntimeError, match="genuine"):
        guard.assert_named_lane("evidence/forward.jsonl", "mock")


def test_daily_script_refuses_a_mock_write_to_the_genuine_log(tmp_path: Path) -> None:
    target = tmp_path / "evidence" / "invest-v2" / "forward.jsonl"
    env = os.environ.copy()
    env["STERLING_EVIDENCE_LANE"] = "mock"
    env["STERLING_SYSTEM"] = "sterling-invest"
    env["STERLING_EXPERIMENT"] = "paper-v2"
    env.pop("XAI_API_KEY", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_invest_daily.py"),
            "--log",
            str(target),
            "--session",
            "2026-11-12",
            "--label",
            "SYNTHETIC",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 2
    assert "genuine" in completed.stderr
    assert not target.exists()
    v1 = tmp_path / "evidence" / "forward.jsonl"
    assert not v1.exists()


def _guard():
    path = ROOT / "deploy" / "private-runner" / "run_private.py"
    spec = importlib.util.spec_from_file_location("lane_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

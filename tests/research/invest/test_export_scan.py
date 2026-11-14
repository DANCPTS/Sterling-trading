"""The export scan reads the API key from the environment, never from argv."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.invest.book import Book
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.schema import CASH, FUNDS, allocation_json

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "export_research_run.sh"
AS_OF = datetime(2026, 11, 12, 16, 30, tzinfo=UTC)
KEY = "sentinel-key-9f3a-NOT-A-REAL-KEY"


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")


def _log(tmp_path: Path) -> Path:
    return tmp_path / "evidence" / "invest-v2" / "mock" / "forward.jsonl"


def _write(log: Path, reasoning: str) -> None:
    weights = {name: "0" for name in FUNDS}
    weights[CASH] = "1"
    raw = allocation_json(
        weights,
        model_id="grok-4.3",
        decision="HOLD",
        portfolio_reasoning=reasoning,
        change_justification="Export scan fixture.",
        identified_risks=("the scan must not leak the key",),
    )
    run_invest_week(
        log_path=log,
        book=Book(),
        prices={name: Decimal("100") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=AS_OF,
        provider=MockGrokProvider(steps=(ScriptStep(raw_response=raw, model_id="grok-4.3"),), response_format="allocation"),
        prompt="export scan",
        mode="simulated_clock",
    )


def _destinations(name: str) -> list[Path]:
    return [Path("/opt/cursor/artifacts") / name, Path("/cursor/stores/self") / name]


def _remove(name: str) -> None:
    for path in _destinations(name):
        if path.exists():
            shutil.rmtree(path)


def _run(
    log: Path, name: str, *, key: str | None, shim: Path | None = None, argv_log: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("STERLING_PUBLISH_PY", None)
    env.pop("STERLING_EVIDENCE_CLONE", None)
    if key is None:
        env.pop("XAI_API_KEY", None)
    else:
        env["XAI_API_KEY"] = key
    if shim is not None:
        env["PATH"] = f"{shim}:{env.get('PATH', '')}"
    if argv_log is not None:
        env["EXPORT_ARGV_LOG"] = str(argv_log)
    return subprocess.run(
        ["bash", str(SCRIPT), str(log), name],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _shim(directory: Path) -> None:
    directory.mkdir()
    real_python = sys.executable
    for name in ("python3", "python", "grep"):
        real = real_python if name.startswith("python") else "/usr/bin/grep"
        path = directory / name
        path.write_text(
            f'#!/bin/bash\nprintf "%s\\0" "$0" "$@" >> "$EXPORT_ARGV_LOG"\nexec {real} "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)


def test_planted_key_blocks_export_and_stays_off_argv(stamped: None, tmp_path: Path) -> None:
    del stamped
    planted = _log(tmp_path / "planted")
    clean = _log(tmp_path / "clean")
    _write(planted, f"do not export {KEY}")
    _write(clean, "A clean recorded hold.")
    assert KEY in planted.read_text(encoding="utf-8")
    blocked_name = "pytest-export-blocked-sentinel"
    clean_name = "pytest-export-clean-sentinel"
    argv_log = tmp_path / "argv.bin"
    shim = tmp_path / "shim"
    _shim(shim)
    _remove(blocked_name)
    _remove(clean_name)
    try:
        blocked = _run(planted, blocked_name, key=KEY, shim=shim, argv_log=argv_log)
        assert blocked.returncode != 0
        assert KEY not in blocked.stdout
        assert KEY not in blocked.stderr
        assert "export aborted: API key text is in the log" in blocked.stderr
        for path in _destinations(blocked_name):
            assert not path.exists()
        recorded = argv_log.read_bytes()
        assert KEY.encode("utf-8") not in recorded
        clean_run = _run(clean, clean_name, key=KEY, shim=shim, argv_log=argv_log)
        assert clean_run.returncode == 0, clean_run.stderr
        assert KEY not in clean_run.stdout
        assert KEY not in clean_run.stderr
        assert KEY.encode("utf-8") not in argv_log.read_bytes()
        for path in _destinations(clean_name):
            assert path.is_dir()
            for item in path.rglob("*"):
                if item.is_file():
                    assert KEY.encode("utf-8") not in item.read_bytes()
            subprocess.run(["sha256sum", "-c", "MANIFEST.sha256"], cwd=path, check=True, capture_output=True)
    finally:
        _remove(blocked_name)
        _remove(clean_name)


def test_unset_key_still_blocks_authorization_header(stamped: None, tmp_path: Path) -> None:
    del stamped
    log = _log(tmp_path)
    _write(log, "header word Bearer must block the export")
    name = "pytest-export-bearer-sentinel"
    _remove(name)
    try:
        completed = _run(log, name, key=None)
        assert completed.returncode != 0
        assert "Authorization or Bearer" in completed.stderr
        for path in _destinations(name):
            assert not path.exists()
    finally:
        _remove(name)

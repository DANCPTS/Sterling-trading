"""Offline rehearsal of the private runner. No network and no real secret."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sterling.research.fx.grok_forward import ProposalLog

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "deploy" / "private-runner"
SECRET_KEY = "synthetic-key-not-real-99"
SECRET_URL = "https://hc.example/ping/secret-token-77"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True)


def _app_commit(tmp: Path) -> tuple[Path, str]:
    source = tmp / "source"
    source.mkdir()
    shutil.copytree(REPO / "src", source / "src")
    shutil.copytree(REPO / "scripts", source / "scripts")
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "sterling@example.com")
    _git(source, "config", "user.name", "Sterling")
    _git(source, "add", "src", "scripts")
    _git(source, "commit", "-m", "Pin the application")
    sha = _git(source, "rev-parse", "HEAD").stdout.strip()
    bare = tmp / "public.git"
    subprocess.run(["git", "clone", "--bare", str(source), str(bare)], check=True, capture_output=True, text=True)
    app = tmp / "app"
    subprocess.run(["git", "clone", str(bare), str(app)], check=True, capture_output=True, text=True)
    return app, sha


def _private(tmp: Path) -> Path:
    work = tmp / "private-seed"
    work.mkdir()
    _git(work, "init", "-b", "evidence")
    _git(work, "config", "user.email", "sterling@example.com")
    _git(work, "config", "user.name", "Sterling")
    _git(work, "commit", "--allow-empty", "-m", "Start evidence")
    bare = tmp / "private.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True, text=True)
    clone = tmp / "private"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True, text=True)
    _git(clone, "config", "user.email", "sterling@example.com")
    _git(clone, "config", "user.name", "Sterling")
    return clone


def test_provider_selection_never_falls_back_silently() -> None:
    module = _load("run_private", RUNNER / "run_private.py")
    assert module.select_provider("schedule", "mock") == "xai"
    assert module.select_provider("workflow_dispatch", "") == "mock"
    assert module.select_provider("workflow_dispatch", "xai") == "xai"
    with pytest.raises(RuntimeError, match="provider"):
        module.select_provider("workflow_dispatch", "grok")
    with pytest.raises(RuntimeError, match="XAI_API_KEY"):
        module.require_xai_key("xai")


def test_ping_does_not_print_the_secret(capsys: pytest.CaptureFixture[str]) -> None:
    module = _load("ping_healthchecks", RUNNER / "ping_healthchecks.py")
    seen: list[str] = []
    module._open = seen.append
    os.environ["HEALTHCHECKS_PING_URL"] = SECRET_URL
    try:
        assert module.main(["--fail"]) == 0
    finally:
        os.environ.pop("HEALTHCHECKS_PING_URL", None)
    assert seen == [SECRET_URL + "/fail"]
    captured = capsys.readouterr()
    assert "secret-token-77" not in captured.out
    assert "secret-token-77" not in captured.err
    assert "healthchecks ping fail" in captured.out


def test_pinned_checkout_two_runs_and_a_failed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, sha = _app_commit(tmp_path)
    private = _private(tmp_path)
    pin = tmp_path / "pinned_app_sha.txt"
    pin.write_text(sha + "\n", encoding="utf-8")
    module = _load("run_private", RUNNER / "run_private.py")
    monkeypatch.setenv("XAI_API_KEY", SECRET_KEY)
    monkeypatch.setenv("HEALTHCHECKS_PING_URL", SECRET_URL)
    args = [
        "--app",
        str(app),
        "--evidence",
        str(private),
        "--event",
        "workflow_dispatch",
        "--provider",
        "mock",
        "--pin-file",
        str(pin),
        "--as-of",
        "2026-11-02T16:30:00+00:00",
    ]
    assert module.run(args) == 0
    again = module.run(args)
    assert again == 0
    log_path = private / "evidence" / "forward.jsonl"
    text = log_path.read_text(encoding="utf-8")
    assert SECRET_KEY not in text
    assert "secret-token-77" not in text
    assert text.count(sha) >= 2
    assert ProposalLog(log_path).verify() >= 2
    before = text
    monkeypatch.delenv("XAI_API_KEY")
    failed = module.main(
        [
            "--app",
            str(app),
            "--evidence",
            str(private),
            "--event",
            "schedule",
            "--provider",
            "mock",
            "--pin-file",
            str(pin),
        ]
    )
    assert failed == 1
    assert log_path.read_text(encoding="utf-8") == before
    monkeypatch.setenv("XAI_API_KEY", SECRET_KEY)
    monkeypatch.setenv("STERLING_XAI_OFFLINE", "1")
    paid = module.run(
        [
            "--app",
            str(app),
            "--evidence",
            str(private),
            "--event",
            "workflow_dispatch",
            "--provider",
            "xai",
            "--pin-file",
            str(pin),
            "--allow-offline-xai",
            "--as-of",
            "2026-11-16T16:30:00+00:00",
        ]
    )
    assert paid == 0
    stored = log_path.read_text(encoding="utf-8")
    assert SECRET_KEY not in stored
    assert "api.x.ai" not in stored
    usage = [json.loads(line) for line in stored.splitlines() if '"model_usage"' in line]
    assert usage
    assert usage[-1]["cost_in_usd_ticks"] == 0

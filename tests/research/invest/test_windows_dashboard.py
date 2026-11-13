"""Windows launcher import: mock lane, fail closed, and an old stored return."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.invest.import_dashboard import import_verified
from sterling.research.invest.report import write_dashboard
from sterling.research.invest.schema import FUNDS

ROOT = Path(__file__).resolve().parents[3]
WHEN = datetime(2026, 11, 12, 16, 30, tzinfo=UTC)
_ORIGIN = "https://github.com/DANCPTS/sterling-forward-evidence.git"


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")


def test_mock_import_flags_the_old_baseline_and_keeps_the_last_page(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "mock.jsonl"
    _old_baseline_log(log)
    page = tmp_path / "dashboard.html"
    page.write_text("previous", encoding="utf-8")
    assert import_verified(log, page, lane="mock", evidence_sha="abc123", fetched="fresh") == 0
    html = page.read_text(encoding="utf-8")
    assert "MOCK / PAPER" in html
    assert "GENUINE PAPER" not in html
    assert "Yahoo market prices (delayed, not exchange-verified)" in html
    assert "SYNTHETIC fixture" not in html
    assert "does not match NAV against starting capital" in html
    assert "2.00%" in html
    assert "3.03%" in html
    assert html.count("BUY") >= 6
    assert b"\xc2\xa3" in page.read_bytes()
    broken = tmp_path / "broken.jsonl"
    broken.write_text(log.read_text(encoding="utf-8").replace("paper-v2", "paper-v9", 1), encoding="utf-8")
    assert import_verified(broken, page, lane="mock", evidence_sha="abc123") == 1
    kept = page.read_text(encoding="utf-8")
    assert "CHECK FAILED" in kept
    assert "Fetched origin/evidence" not in kept
    assert "Evidence commit abc123" in kept
    assert "Last valuation 2026-11-13" in kept
    assert "2.00%" in kept
    assert "MOCK / PAPER" in kept
    forward = tmp_path / "forward.jsonl"
    _old_baseline_log(forward, lane="forward")
    assert import_verified(forward, page, lane="mock", evidence_sha="abc123") == 1
    assert "CHECK FAILED" in page.read_text(encoding="utf-8")
    assert "2.00%" in page.read_text(encoding="utf-8")


def test_launcher_syntax_and_fixture_repo(tmp_path: Path, stamped: None) -> None:
    del stamped
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    for name in ("sterling-dashboard.ps1", "setup.ps1", "sterling-python.ps1"):
        _parse(pwsh, ROOT / "deploy" / "windows" / name)
    remote, clone = _evidence_repo(tmp_path)
    local = tmp_path / "local"
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local)
    env["PYTHONPATH"] = str(ROOT / "src")
    before = set(Path("/tmp").glob("sterling-evidence-*.jsonl"))
    completed = _launch(pwsh, clone, env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "verified=OK" in completed.stdout
    assert "Fetched origin/evidence this run." in completed.stdout
    ssh = _launch_with_origin(pwsh, clone, env, "git@github.com:DANCPTS/sterling-forward-evidence.git", remote)
    assert ssh.returncode == 0, ssh.stdout + ssh.stderr
    assert "Fetched origin/evidence this run." in ssh.stdout
    assert _branch(clone, env) == "update/paper-v2-runner"
    assert "records=" in completed.stdout
    page = local / "SterlingTrading" / "dashboard.html"
    fresh = page.read_text(encoding="utf-8")
    assert "MOCK / PAPER" in fresh
    assert "Fetched origin/evidence" in fresh
    assert "OFFLINE / STALE" not in fresh
    assert _branch(clone, env) == "update/paper-v2-runner"
    assert _status(clone, env) == ""
    assert not (local / "SterlingTrading" / "forward.jsonl").exists()
    assert set(Path("/tmp").glob("sterling-evidence-*.jsonl")) <= before
    _git(clone, "config", "--unset-all", f"url.{remote}/.insteadOf", env=env)
    _git(clone, "config", f"url.{tmp_path / 'missing.git'}.insteadOf", _ORIGIN, env=env)
    offline = _launch(pwsh, clone, env)
    assert offline.returncode == 0, offline.stdout + offline.stderr
    assert "OFFLINE / STALE" in offline.stdout
    assert "verified=OK" in offline.stdout
    stale = page.read_text(encoding="utf-8")
    assert "OFFLINE / STALE" in stale
    assert "Do not treat it as a new fetch." in stale
    assert _branch(clone, env) == "update/paper-v2-runner"
    kept = stale
    subprocess.run(
        ["git", "-C", str(clone), "remote", "set-url", "origin", "https://github.com/DANCPTS/Sterling-trading.git"],
        check=True,
        env=env,
    )
    del remote
    refused = _launch(pwsh, clone, env)
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "Refusing to read this clone" in refused.stdout
    assert page.read_text(encoding="utf-8") == kept
    assert not list(local.rglob("*.jsonl"))


def test_interrupted_write_keeps_the_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamped: None) -> None:
    del stamped
    log = tmp_path / "mock.jsonl"
    _old_baseline_log(log)
    page = tmp_path / "dashboard.html"
    page.write_text("kept", encoding="utf-8")
    real_write = Path.write_text

    def boom(self: Path, data: str, encoding: str | None = None, errors: str | None = None, newline: str | None = None) -> int:
        if self.name.endswith(".tmp"):
            raise OSError("disk")
        return real_write(self, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", boom)
    assert write_dashboard(log, page) == 1
    assert page.read_text(encoding="utf-8") == "kept"
    assert not list(tmp_path.glob("*.tmp"))


def _old_baseline_log(path: Path, *, lane: str = "mock") -> None:
    os.environ["STERLING_EVIDENCE_LANE"] = lane
    log = ProposalLog(path)
    log.create(now=WHEN)
    moment = WHEN + timedelta(minutes=1)
    actions = {name: "BUY" for name in FUNDS}
    log.append_event(
        timestamp_utc=moment,
        record_type="decision",
        fields={
            "accepted_weights": {name: "0.10" for name in FUNDS} | {"CASH": "0.40"},
            "cash_gbp": "10000.00",
            "derived_actions": actions,
            "execution_permission": "NOT_ISSUED",
            "gate": "accepted",
        },
    )
    for index, name in enumerate(FUNDS, start=2):
        log.append_event(
            timestamp_utc=WHEN + timedelta(minutes=index),
            record_type="fill",
            fields={
                "cash_delta_gbp": "-101.00",
                "commission_gbp": "0.00",
                "fund": name,
                "fx_cost_gbp": "0.00",
                "price_gbp": "100.00",
                "session": "2026-11-12",
                "side": "BUY",
                "slippage_gbp": "0.00",
                "spread_gbp": "0.00",
                "stamp_gbp": "0.00",
                "units": "1",
            },
        )
    log.append_event(
        timestamp_utc=WHEN + timedelta(hours=1),
        record_type="valuation",
        fields=_valuation("2026-11-12", "9900.00", "-100.00", "-0.01", "10000.00", "0.01"),
    )
    stored = format(Decimal("10200.00") / Decimal("9900.00") - 1, "f")
    log.append_event(
        timestamp_utc=WHEN + timedelta(days=1),
        record_type="valuation",
        fields=_valuation("2026-11-13", "10200.00", "300.00", stored, "10200.00", "0.01"),
    )


def _valuation(session: str, nav: str, daily: str, cumulative: str, high: str, drawdown: str) -> dict[str, object]:
    return {
        "cash_gbp": nav,
        "cumulative_costs_gbp": "0.00",
        "cumulative_return": cumulative,
        "daily_pnl_gbp": daily,
        "data_label": "live",
        "drawdown": drawdown,
        "high_water_gbp": high,
        "holdings": {},
        "nav_gbp": nav,
        "prices": {},
        "session": session,
    }


def _parse(pwsh: str, path: Path) -> None:
    script = (
        "$errors = $null; $tokens = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errors); "
        "if ($errors -and $errors.Count -gt 0) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
    )
    completed = subprocess.run([pwsh, "-NoProfile", "-Command", script], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _evidence_repo(tmp_path: Path) -> tuple[Path, Path]:
    env = _git_env(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main", env=env)
    (source / "README").write_text("private\n", encoding="utf-8")
    _git(source, "add", "README", env=env)
    _git(source, "commit", "-m", "main", env=env)
    _git(source, "checkout", "-b", "evidence", env=env)
    log = source / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    _old_baseline_log(log)
    _git(source, "add", "evidence", env=env)
    _git(source, "commit", "-m", "mock evidence", env=env)
    _git(source, "checkout", "main", env=env)
    _git(source, "checkout", "-b", "update/paper-v2-runner", env=env)
    remote = tmp_path / "remote.git"
    _git(source, "clone", "--bare", str(source), str(remote), env=env)
    clone = tmp_path / "clone"
    _git(source, "clone", str(remote), str(clone), env=env)
    _git(clone, "remote", "set-url", "origin", _ORIGIN, env=env)
    _git(clone, "config", f"url.{remote}/.insteadOf", _ORIGIN, env=env)
    return remote, clone


def _launch_with_origin(
    pwsh: str,
    clone: Path,
    env: dict[str, str],
    origin: str,
    remote: Path,
) -> subprocess.CompletedProcess[str]:
    _git(clone, "remote", "set-url", "origin", origin, env=env)
    _git(clone, "config", f"url.{remote}/.insteadOf", origin, env=env)
    return _launch(pwsh, clone, env)


def _launch(pwsh: str, clone: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    script = ROOT / "deploy" / "windows" / "sterling-dashboard.ps1"
    return subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(script),
            "-AppRoot",
            str(ROOT),
            "-EvidenceRoot",
            str(clone),
            "-PythonExe",
            sys.executable,
            "-NoBrowser",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _git_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "sterling-forward-runner",
            "GIT_AUTHOR_EMAIL": "sterling-forward-runner@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "sterling-forward-runner",
            "GIT_COMMITTER_EMAIL": "sterling-forward-runner@users.noreply.github.com",
        }
    )
    return env


def _git(repo: Path, *args: str, env: dict[str, str]) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)


def _branch(repo: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _status(repo: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()

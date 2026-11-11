"""Offline rehearsal of the private-runner entrypoints. Mock only. No xAI call."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sterling.research.invest.report import write_dashboard
from sterling.research.money import quantize_money

REPO = Path(__file__).resolve().parents[3]
FUNDS = ("VWRP", "CUKX", "IGLT", "AGBP", "SGLN", "XSTR")
DAYS = [date(2026, 11, day) for day in (2, 3, 4, 5, 6, 9, 10, 12, 13, 16)]
W46 = (
    b"run7-fragments-not-the-private-file\n"
    b"state_hash=38e0ccb2a8a9cac2de1bcee844efeca4ec8b70b52e1e1eb0d6ed7820e1bc31a9\n"
    b"request_hash=ad75958d30afde3a557ec613bf2d7e1dd725ac6d7e1701345229bfea1eb28db8\n"
    b"gate=hash_mismatch week=2026-W46\n"
)


def test_paper_v2_runner_rehearsal(tmp_path: Path) -> None:
    rules = hashlib.sha256((REPO / "src/sterling/rules/data/desk-rules-v0.2.json").read_bytes()).hexdigest()
    policy = hashlib.sha256((REPO / "src/sterling/risk/data/stage2-gate1-policy-v1.json").read_bytes()).hexdigest()
    assert rules == "ba938baa22dd74944c9fe2cc7e68182c06498b3bc5fbff388df84818f6ae24ba"
    assert policy == "5b5c315e57ebf65c3cd7291af11fd7cbe0bc897b2ee7d57f7a8051030246a363"
    _assert_cron_disabled()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    (tmp_path / "home").mkdir()
    assert "XAI_API_KEY" not in env
    app, sha = _app(tmp_path, env)
    private, bare = _private(tmp_path, env)
    pin = private / "deploy" / "private-runner" / "pinned_app_sha.txt"
    pin.write_text(sha + "\n", encoding="utf-8")
    w46 = tmp_path / "w46-stand-in.jsonl"
    w46.write_bytes(W46)
    before = hashlib.sha256(w46.read_bytes()).hexdigest()
    sonia = tmp_path / "sonia.csv"
    sonia.write_text("DATE,IUDSOIA\n30 Oct 2026,4.00\n20 Nov 2026,9.00\n", encoding="utf-8")
    notes: list[tuple[str, str, str]] = []

    def check(item: str, ok: bool, detail: str) -> None:
        notes.append((item, "pass" if ok else "fail", detail))
        assert ok, detail

    first = _weekly(private, app, pin, env, "2026-11-02T16:30:00+00:00")
    check("1 start and 2 mock decision", first.returncode == 0 and "provider_calls=1" in first.stdout, first.stderr)
    rerun = _weekly(private, app, pin, env, "2026-11-02T16:30:00+00:00")
    check("8 weekly rerun", rerun.returncode == 0 and "provider_calls=0" in rerun.stdout, rerun.stderr)
    _crash_then_restart(private, app, env, sonia, tmp_path)
    log = private / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    skipped = log.read_text(encoding="utf-8").count("opening_price_unavailable")
    check("3 missing open stays pending", skipped == 1, f"skips={skipped}")
    for day in DAYS:
        if day == date(2026, 11, 2):
            continue
        if day == date(2026, 11, 3):
            continue
        if day == date(2026, 11, 9):
            second = _weekly(private, app, pin, env, "2026-11-09T16:30:00+00:00")
            check("2 second week", second.returncode == 0 and "provider_calls=1" in second.stdout, second.stderr)
            third = _weekly(private, app, pin, env, "2026-11-09T16:30:00+00:00")
            check("8 second week rerun", third.returncode == 0 and "provider_calls=0" in third.stdout, third.stderr)
        result = _daily(private, app, pin, env, day, sonia, skip=())
        check(f"4 daily {day.isoformat()}", result.returncode == 0 and "provider_calls=0" in result.stdout, result.stderr)
    stale = [
        line for line in log.read_text(encoding="utf-8").splitlines() if '"session":"2026-11-12"' in line and "valuation" in line
    ]
    check("4 stale price flagged", bool(stale) and '"status":"stale"' in stale[0] and "AGBP" in stale[0], "stale row missing")
    again = _daily(private, app, pin, env, date(2026, 11, 13), sonia, skip=())
    check("8 same-day rerun", again.returncode == 0 and "status=noop" in again.stdout, again.stdout + again.stderr)
    text = log.read_text(encoding="utf-8")
    check("10 no provider record", '"record_type":"model_usage"' not in text and "api.x.ai" not in text, "provider residue")
    check("9 W46 stand-in", hashlib.sha256(w46.read_bytes()).hexdigest() == before, before)
    forward = private / "evidence" / "forward.jsonl"
    invest_forward = private / "evidence" / "invest-v2" / "forward.jsonl"
    check("9 genuine paths untouched", not forward.exists() and not invest_forward.exists(), "a forward log was created")
    remote = subprocess.run(
        ["git", "--git-dir", str(bare), "show", "evidence:evidence/invest-v2/mock/forward.jsonl"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    check("7 published chain", remote.returncode == 0 and remote.stdout == text, remote.stderr)
    verified = _verify(private, log, env)
    check("7 verifier", verified.returncode == 0, verified.stderr)
    figures = _reconcile(text)
    check("5 accounting", figures["ok"], figures["detail"])
    page = tmp_path / "dashboard.html"
    check("6 dashboard", write_dashboard(log, page) == 0 and "%" in page.read_text(encoding="utf-8"), "dashboard")
    html = page.read_text(encoding="utf-8")
    formatted = "%" in html and "," in html and "0.00000" not in html
    check("6 formatting", formatted, "money or percent format missing")
    kept = page.read_text(encoding="utf-8")
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text(text.replace("paper-v2", "paper-v9", 1), encoding="utf-8")
    deleted = tmp_path / "deleted.jsonl"
    body = text.splitlines()
    del body[4]
    deleted.write_text("\n".join(body) + "\n", encoding="utf-8")
    lines = text.splitlines()
    lines[2], lines[3] = lines[3], lines[2]
    reordered = tmp_path / "reordered.jsonl"
    reordered.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for path in (tampered, deleted, reordered):
        failed = _verify(private, path, env)
        check(f"7 tamper {path.name}", failed.returncode != 0, "tamper was accepted")
        assert write_dashboard(path, page) == 1
    check("7 dashboard not updated", page.read_text(encoding="utf-8") == kept, "dashboard changed")
    decisions = text.count('"record_type":"decision"')
    check("8 one decision per week", decisions == 2, f"decisions={decisions}")
    decisions_now = [line for line in log.read_text(encoding="utf-8").splitlines() if '"record_type":"decision"' in line]
    carried = len(decisions_now) == 2 and '"cash_gbp":"10000.00"' not in decisions_now[1]
    check("8 book carried into week 2", carried, "week 2 restarted from cash")
    _write_report(notes, figures, page, log)
    check("cron still disabled", True, "workflows")


def _crash_then_restart(private: Path, app: Path, env: dict[str, str], sonia: Path, tmp_path: Path) -> None:
    day = date(2026, 11, 3)
    script = tmp_path / "crash.py"
    log_path = private / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    script.write_text(_crash_source(log_path, sonia), encoding="utf-8")
    child = {
        **env,
        "PYTHONPATH": str(app / "src"),
        "STERLING_SYSTEM": "sterling-invest",
        "STERLING_EXPERIMENT": "paper-v2",
        "STERLING_EVIDENCE_LANE": "mock",
    }
    crashed = subprocess.run([sys.executable, str(script)], check=False, capture_output=True, text=True, env=child)
    assert crashed.returncode == 0, crashed.stderr
    log = private / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    fills = log.read_text(encoding="utf-8").count('"record_type":"fill"')
    restarted = _daily(private, app, private / "deploy" / "private-runner" / "pinned_app_sha.txt", env, day, sonia, skip=("SGLN",))
    assert restarted.returncode == 0, restarted.stderr + restarted.stdout
    text = log.read_text(encoding="utf-8")
    assert text.count('"record_type":"fill"') == fills
    assert text.count('"session":"2026-11-03"') and text.count('"record_type":"valuation"') >= 1


def _crash_source(log_path: Path, sonia: Path) -> str:
    return "\n".join(
        [
            "from datetime import UTC, date, datetime",
            "from decimal import Decimal",
            "from pathlib import Path",
            "from sterling.research.invest.cash_rate import parse_sonia_csv",
            "from sterling.research.invest.daily import run_daily",
            f"log = Path({str(log_path)!r})",
            f"sonia = parse_sonia_csv(Path({str(sonia)!r}).read_text())",
            "names = ('VWRP', 'CUKX', 'IGLT', 'AGBP', 'XSTR', 'SGLN')",
            "opens = {name: Decimal('101') for name in names if name != 'SGLN'}",
            "history = {name: ((date(2026, 11, 3), Decimal('101')),) for name in names}",
            "run_daily(",
            "    log_path=log,",
            "    session=date(2026, 11, 3),",
            "    opens=opens,",
            "    close_history=history,",
            "    sonia=sonia,",
            "    recorded_at=datetime(2026, 11, 3, 17, tzinfo=UTC),",
            "    open_source='SYNTHETIC-open',",
            "    data_label='SYNTHETIC',",
            "    stop_after='fills',",
            ")",
            "",
        ]
    )


def _weekly(private: Path, app: Path, pin: Path, env: dict[str, str], as_of: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(private / "deploy" / "private-runner" / "run_private.py"),
            "--app",
            str(app),
            "--evidence",
            str(private),
            "--event",
            "workflow_dispatch",
            "--provider",
            "mock",
            "--policy",
            "paper-v2",
            "--pin-file",
            str(pin),
            "--as-of",
            as_of,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _daily(
    private: Path,
    app: Path,
    pin: Path,
    env: dict[str, str],
    day: date,
    sonia: Path,
    skip: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(private / "deploy" / "private-runner" / "run_daily.py"),
            "--app",
            str(app),
            "--evidence",
            str(private),
            "--pin-file",
            str(pin),
            "--session",
            day.isoformat(),
            "--lane",
            "mock",
            "--label",
            "SYNTHETIC",
            "--opens",
            _opens(day, skip),
            "--closes",
            _closes(day),
            "--sonia-csv",
            str(sonia),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _verify(private: Path, log: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(private / "deploy" / "private-runner" / "verify_from_log.py"), "--log", str(log)],
        check=False,
        capture_output=True,
        text=True,
        env={**env, "PYTHONPATH": str(REPO / "src")},
    )


def _opens(day: date, skip: tuple[str, ...]) -> str:
    return ",".join(f"{name}={100 + DAYS.index(day)}" for name in FUNDS if name not in skip)


def _closes(day: date) -> str:
    parts: list[str] = []
    for name in FUNDS:
        for seen in DAYS:
            if seen > day:
                continue
            if name == "AGBP" and seen >= date(2026, 11, 12):
                continue
            parts.append(f"{name}@{seen.isoformat()}={100 + DAYS.index(seen)}")
    return ",".join(parts)


def _reconcile(text: str) -> dict[str, object]:
    import json

    cash = Decimal("10000.00")
    quantities = {name: Decimal(0) for name in FUNDS}
    costs = Decimal("0.00")
    interest = Decimal("0.00")
    detail = "ok"
    ok = True
    for line in text.splitlines():
        record = json.loads(line)
        kind = record.get("record_type")
        if kind == "cash_interest":
            rate = Decimal(str(record["annual_decimal"]))
            expected = quantize_money(cash * rate / Decimal(365))
            if Decimal(str(record["amount_gbp"])) != expected or "9.00" in str(record.get("sonia_percent")):
                ok = False
                detail = f"interest {record['session']} {record['amount_gbp']} != {expected}"
            if record.get("published_on") and str(record["published_on"]) > str(record["session"]):
                ok = False
                detail = "look-ahead SONIA"
            cash += Decimal(str(record["amount_gbp"]))
            interest += Decimal(str(record["amount_gbp"]))
        elif kind == "fill":
            cash += Decimal(str(record["cash_delta_gbp"]))
            units = Decimal(str(record["units"]))
            quantities[str(record["fund"])] += units if record["side"] == "BUY" else -units
            parts = ("spread_gbp", "slippage_gbp", "stamp_gbp", "commission_gbp", "fx_cost_gbp")
            costs += sum(Decimal(str(record[key])) for key in parts)
        elif kind == "valuation":
            marked = cash
            for name, units in quantities.items():
                info = record["prices"][name]
                if units != 0:
                    marked += units * Decimal(str(info["price_gbp"]))
            if abs(marked - Decimal(str(record["nav_gbp"]))) > Decimal("0.05"):
                ok = False
                detail = f"nav {record['session']} {marked} != {record['nav_gbp']}"
            if Decimal(str(record["cumulative_costs_gbp"])) != costs:
                ok = False
                detail = f"costs {record['cumulative_costs_gbp']} != {costs}"
    last = json.loads([line for line in text.splitlines() if '"record_type":"valuation"' in line][-1])
    trades = text.count('"record_type":"fill"')
    return {
        "ok": ok,
        "detail": detail,
        "nav": last["nav_gbp"],
        "cash": last["cash_gbp"],
        "costs": format(costs, "f"),
        "interest": format(interest, "f"),
        "trades": trades,
        "drawdown": last["drawdown"],
    }


def _write_report(notes: list[tuple[str, str, str]], figures: dict[str, object], page: Path, log: Path) -> None:
    destination = Path("/opt/cursor/artifacts")
    if not destination.is_dir():
        return
    shutil.copyfile(page, destination / "dashboard.html")
    lines = [
        "# Sterling Invest paper-v2 offline rehearsal",
        "",
        "DATA LABEL: SYNTHETIC/MOCK. No xAI call.",
        "",
        "| Item | Result | Detail |",
        "|---|---|---|",
    ]
    for item, result, detail in notes:
        lines.append(f"| {item} | {result} | {detail.replace('|', '/')} |")
    lines.extend(
        [
            "",
            f"End value {figures['nav']}. Cash {figures['cash']}. Trades {figures['trades']}.",
            f"Costs {figures['costs']}. Interest {figures['interest']}. Drawdown {figures['drawdown']}.",
            "",
            "Yahoo daily opens are the live source and are not cross-checked against the exchange.",
        ]
    )
    (destination / "rehearsal-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    del log


def _assert_cron_disabled() -> None:
    root = REPO / "deploy" / "private-runner" / ".github" / "workflows"
    for name in ("weekly-forward.yml", "daily-invest.yml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "\n    if: false\n" in text
        for line in text.splitlines():
            stripped = line.strip()
            if "cron:" in stripped:
                assert stripped.startswith("#")


def _app(tmp: Path, env: dict[str, str]) -> tuple[Path, str]:
    source = tmp / "source"
    shutil.copytree(REPO / "src", source / "src")
    shutil.copytree(REPO / "scripts", source / "scripts")
    guard = source / "src" / "sitecustomize.py"
    guard.write_text(
        "import socket\n"
        "_socket = socket.socket\n"
        "class _Guard(_socket):\n"
        "    def connect(self, address):\n"
        "        host = address[0] if isinstance(address, tuple) else ''\n"
        "        if host not in {'127.0.0.1', 'localhost', '::1'}:\n"
        "            raise OSError('provider network blocked')\n"
        "        return super().connect(address)\n"
        "socket.socket = _Guard\n",
        encoding="utf-8",
    )
    _git(source, "init", "-b", "main", env=env)
    _git(source, "add", "src", "scripts", env=env)
    _git(source, "commit", "-m", "App", env=env)
    sha = _git(source, "rev-parse", "HEAD", env=env).stdout.strip()
    app = tmp / "app"
    subprocess.run(["git", "clone", str(source), str(app)], check=True, capture_output=True, text=True, env=env)
    return app, sha


def _private(tmp: Path, env: dict[str, str]) -> tuple[Path, Path]:
    seed = tmp / "seed"
    seed.mkdir()
    shutil.copytree(REPO / "deploy" / "private-runner", seed / "deploy" / "private-runner")
    _git(seed, "init", "-b", "main", env=env)
    _git(seed, "add", "deploy", env=env)
    _git(seed, "commit", "-m", "Runner", env=env)
    _git(seed, "branch", "evidence", env=env)
    bare = tmp / "private.git"
    subprocess.run(["git", "clone", "--bare", str(seed), str(bare)], check=True, capture_output=True, text=True, env=env)
    private = tmp / "private"
    subprocess.run(["git", "clone", str(bare), str(private)], check=True, capture_output=True, text=True, env=env)
    return private, bare


def _git(repo: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=sterling-forward-runner",
            "-c",
            "user.email=sterling-forward-runner@users.noreply.github.com",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

"""Two labelled weekly runs, then a no-op rerun. No xAI call.

The two-week log uses mode=simulated_clock. Its decision, request and response
times come from the injected --as-of clock. recorded_at is datetime.now(UTC)
at the moment the process writes the line. A separate forward log, if produced,
uses the system clock for the decision itself. Prices are fixture data.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.timekeeping import week_key
from sterling.research.grok.verify import verify_modes
from sterling.research.grok.weekly import flag_missed_weeks as write_missed

ROOT = Path(__file__).resolve().parent / "run_weekly_decision.py"


def _run(log: Path, as_of: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT), "--fixture", "--simulated-clock", "--provider", "mock", "--as-of", as_of, "--log", str(log)],
        check=False,
        text=True,
        capture_output=True,
    )


def main() -> int:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "research-output/two-week-demo")
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "forward.jsonl"
    if log.exists():
        log.unlink()
    state = log.with_suffix(".state.json")
    if state.exists():
        state.unlink()
    genuine = datetime.now(UTC).isoformat()
    first = _run(log, "2026-11-02T16:30:00+00:00")
    second = _run(log, "2026-11-16T16:30:00+00:00")
    third = _run(log, "2026-11-16T18:00:00+00:00")
    records = ProposalLog(log).records()
    weeks = [str(record.get("week_key")) for record in records if record.get("record_type") == "decision"]
    expected = []
    if weeks:
        expected = [weeks[0], "2026-W46", weeks[-1]]
    missed = write_missed(log, expected, mode="simulated_clock")
    chain = ProposalLog(log).verify()
    mode = verify_modes(ProposalLog(log).records(), now=datetime.now(UTC))
    forward = folder / "forward-now.jsonl"
    cache = folder / "cache"
    cache.mkdir()
    session = datetime.now(UTC).date() - timedelta(days=1)
    while session.weekday() >= 5:
        session -= timedelta(days=1)
    for ticker in ("VWRP", "CUKX", "IGLT", "AGBP", "SGLN", "XSTR"):
        line = f"date,close,quoted_in\n{session.isoformat()},100,GBP\n"
        (cache / f"{ticker}.csv").write_text(line, encoding="utf-8")
        (cache / f"{ticker}.check.csv").write_text(line, encoding="utf-8")
    live = subprocess.run(
        [sys.executable, str(ROOT), "--provider", "mock", "--cache", str(cache), "--log", str(forward)],
        check=False,
        text=True,
        capture_output=True,
    )
    print(f"genuine_wall_clock_at_demo_start={genuine}")
    print(f"forward_now_exit={live.returncode}")
    print(live.stdout.strip())
    for record in ProposalLog(forward).records():
        if record.get("record_type") == "decision":
            print(
                "genuine_forward_decision "
                f"mode={record.get('mode')} decision_at={record.get('decision_at')} "
                f"request_at={record.get('request_at')} response_at={record.get('response_at')} "
                f"recorded_at={record.get('recorded_at')}"
            )
    print(f"run1_exit={first.returncode} run2_exit={second.returncode} rerun_exit={third.returncode}")
    print(first.stdout.strip())
    print(second.stdout.strip())
    print(third.stdout.strip())
    print(f"verifier_chain_records={chain} verifier_mode={mode} missed={','.join(missed) or 'none'}")
    print(f"week_of_first_decision={week_key(datetime.fromisoformat('2026-11-02T16:30:00+00:00'))}")
    for record in ProposalLog(log).records():
        kind = record.get("record_type")
        if kind not in {"week_reserve", "decision", "fill", "state_commit", "missed_week"}:
            continue
        fields = {
            name: record.get(name)
            for name in (
                "record_type",
                "mode",
                "clock_source",
                "week_key",
                "decision_at",
                "request_at",
                "response_at",
                "recorded_at",
                "snapshot_hash",
                "gate",
                "accepted_weights",
                "hold_reasons",
                "ticker",
                "side",
                "quantity",
                "price_gbp",
                "cost_gbp",
                "cash_after_gbp",
                "state_hash",
                "note",
                "record_hash",
            )
            if name in record or name == "record_type"
        }
        fields["record_type"] = kind
        fields["record_hash"] = record.get("record_hash")
        print(fields)
    return 0 if first.returncode == 0 and second.returncode == 0 and third.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

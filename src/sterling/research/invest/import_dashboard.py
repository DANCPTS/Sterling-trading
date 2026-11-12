"""Import one verified paper-v2 log into a dashboard file. No network and no orders.

The Windows launcher calls::

    python -m sterling.research.invest.import_dashboard --log LOG --out PAGE --lane mock --evidence-sha SHA

``--lane genuine`` is required before a forward log is accepted. A failed check
does not replace ``--out``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.invest.report import write_dashboard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a verified read-only dashboard. Mock lane unless genuine is named.")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lane", choices=("mock", "genuine"), default="mock")
    parser.add_argument("--evidence-sha", default="")
    parser.add_argument("--fetched", choices=("fresh", "stale"), default="")
    args = parser.parse_args(argv)
    return import_verified(
        args.log,
        args.out,
        lane=args.lane,
        evidence_sha=args.evidence_sha,
        fetched=args.fetched,
    )


def import_verified(
    log_path: Path,
    destination: Path,
    *,
    lane: str,
    evidence_sha: str = "",
    fetched: str = "",
) -> int:
    """Verify the log, then write the page. A failure leaves ``destination`` unchanged."""
    if lane not in {"mock", "genuine"}:
        print("lane must be mock or genuine", file=sys.stderr)
        return 2
    try:
        log = ProposalLog(log_path)
        log.verify()
        records = log.records()
        _require_lane(records, lane)
    except (OSError, ResearchError) as exc:
        _failed(str(exc))
        return 1
    last = _last_valuation(records)
    note = _freshness(fetched, evidence_sha, str(last.get("session") or ""))
    if write_dashboard(log_path, destination, evidence_note=note) != 0:
        _failed("paper-v2 verifier rejected the log")
        return 1
    print(f"evidence_sha={evidence_sha}")
    print(f"records={len(records)}")
    print(f"last_valuation={last.get('session', '')}")
    print(f"nav={last.get('nav_gbp', '')}")
    print("verified=OK")
    return 0


def _require_lane(records: tuple[dict[str, object], ...] | list[dict[str, object]], lane: str) -> None:
    expected = "mock" if lane == "mock" else "forward"
    for record in records:
        if record.get("evidence_lane") != expected:
            raise ResearchError(f"{lane} import refused a {record.get('evidence_lane') or 'unlabelled'} record")


def _last_valuation(records: tuple[dict[str, object], ...] | list[dict[str, object]]) -> dict[str, object]:
    found = [record for record in records if record.get("record_type") == "valuation"]
    return found[-1] if found else {}


def _freshness(fetched: str, evidence_sha: str, session: str) -> str:
    if fetched == "stale":
        return (
            "OFFLINE / STALE. This page was not fetched this run. "
            f"It uses the last origin/evidence {evidence_sha}. Last valuation {session}. "
            "Do not treat it as a new fetch."
        )
    if fetched == "fresh":
        return f"Fetched origin/evidence {evidence_sha} this run. Last valuation {session}."
    return ""


def _failed(reason: str) -> None:
    print("verified=FAILED", file=sys.stderr)
    print(reason, file=sys.stderr)
    print("import refused: evidence did not verify; last page kept", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

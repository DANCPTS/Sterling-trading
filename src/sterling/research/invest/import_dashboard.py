"""Import one verified paper-v2 log into a dashboard file. No network and no orders.

The Windows launcher calls::

    python -m sterling.research.invest.import_dashboard --log LOG --out PAGE --lane mock --evidence-sha SHA

``--lane genuine`` is required before a forward log is accepted. A failed check
does not replace ``--out``.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.invest.dashboard import _esc
from sterling.research.invest.report import write_dashboard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a verified read-only dashboard. Mock lane unless genuine is named.")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lane", choices=("mock", "genuine"), default="mock")
    parser.add_argument("--evidence-sha", default="")
    parser.add_argument("--fetched", choices=("fresh", "stale"), default="")
    parser.add_argument("--mark-kept", action="store_true")
    args = parser.parse_args(argv)
    if args.mark_kept:
        mark_kept(args.out)
        return 1
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
        mark_kept(destination)
        return 1
    last = _last_valuation(records)
    note = _freshness(fetched, evidence_sha, str(last.get("session") or ""))
    if write_dashboard(log_path, destination, evidence_note=note) != 0:
        _failed("paper-v2 verifier rejected the log")
        mark_kept(destination)
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


def mark_kept(destination: Path) -> None:
    """Say the previous page was kept. The figures already in the file stay."""
    if not destination.is_file():
        return
    text = destination.read_text(encoding="utf-8")
    if 'id="evidence-fetch"' not in text:
        return
    previous = re.search(r'<p id="evidence-fetch"[^>]*>(.*?)</p>', text, flags=re.DOTALL)
    body = html.unescape(previous.group(1)) if previous else ""
    sha_match = re.search(r"\b([0-9a-f]{6,40})\b", body)
    session_match = re.search(r"Last valuation ([0-9]{4}-[0-9]{2}-[0-9]{2})", body)
    kept = "CHECK FAILED. The previous verified page was kept and was not replaced. It is not a new fetch."
    if sha_match:
        kept += f" Evidence commit {sha_match.group(1)}."
    if session_match:
        kept += f" Last valuation {session_match.group(1)}."
    banner = f'<p id="evidence-fetch" class="warn">{_esc(kept)}</p>'
    updated = re.sub(r'<p id="evidence-fetch"[^>]*>.*?</p>', banner, text, count=1, flags=re.DOTALL)
    if updated == text:
        return
    from sterling.research.invest.report import _publish_text

    _publish_text(destination, updated)


def _failed(reason: str) -> None:
    print("verified=FAILED", file=sys.stderr)
    print(reason, file=sys.stderr)
    print("import refused: evidence did not verify; last page kept", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

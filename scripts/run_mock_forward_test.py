"""Offline mock of the Grok forward comparison.

No network, no API key, and no broker. The screen is the research pin plus
the Stage 2 Gate 1 assessment. Execution stays DENY.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path

from sterling.decision_boundary import admit_grok_payload, combine_reviews
from sterling.research.grok.compare import run_mock
from sterling.research.grok.safety import pinned_review


def screen(payload: Mapping[str, object]) -> dict[str, object]:
    return combine_reviews(pinned_review(payload), admit_grok_payload(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline Grok forward skeleton.")
    parser.add_argument("--output", type=Path, default=Path("research-output/mock-forward"))
    args = parser.parse_args(argv)
    report = run_mock(args.output, screen=screen)
    sys.stdout.write(report.text)
    print(f"log={report.log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Disabled capture entry point. It does not download prices or SONIA.

A run needs STERLING_CAPTURE_APPROVED=yes and --provider. yahoo and boe-iadb
are refused even then. synthetic reads only the local files you pass.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sterling.research.errors import ResearchError  # noqa: E402
from sterling.research.invest.capture import SyntheticCapture, capture_research_cache  # noqa: E402
from sterling.research.invest.schema import FUNDS  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Prepare a local research cache. No default network.")
    parser.add_argument("--provider", default="")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--session", default="")
    parser.add_argument("--published-at", default="")
    parser.add_argument("--retrieved-at", default="")
    parser.add_argument("--http-date", default="")
    args = parser.parse_args(raw)
    approved = os.environ.get("STERLING_CAPTURE_APPROVED", "")
    try:
        source = None
        if args.provider == "synthetic":
            if args.fixture_dir is None or not args.session or not args.published_at or not args.retrieved_at or not args.http_date:
                print("synthetic capture needs local fixture arguments", file=sys.stderr)
                return 2
            prices = {name: (args.fixture_dir / f"{name}.bin").read_bytes() for name in FUNDS}
            source = SyntheticCapture(
                prices=prices,
                sonia=(args.fixture_dir / "boe_sonia.csv").read_bytes(),
                retrieved_at=datetime.fromisoformat(args.retrieved_at),
                http_date=datetime.fromisoformat(args.http_date),
                session=args.session,
                published_at=args.published_at,
            )
        if args.destination is None:
            print("capture needs --destination", file=sys.stderr)
            return 2
        capture_research_cache(args.destination, approved=approved, provider=args.provider, source=source)
    except (ResearchError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("capture status=written provider=synthetic network=not_used")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""One prospective research day. No model, no network, no official evidence.

The designation is not a flag. The command refuses the run unless the captured
closes and SONIA file satisfy the clock. It does not create a record in this
repository or in the official evidence repository.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sterling.research.errors import ResearchError  # noqa: E402
from sterling.research.invest.prospective import recover_torn_tail, refuse_provider_request, run_prospective  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        refuse_provider_request(raw)
    except ResearchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Value one prospective research day from local captures.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--session")
    parser.add_argument("--inception-session")
    parser.add_argument("--as-of")
    parser.add_argument("--expect-hash")
    parser.add_argument("--expect-records", type=int)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--clock-mode", default="prospective", choices=("prospective", "offline-fixture"))
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args(raw)
    try:
        if args.recover:
            outcome = recover_torn_tail(args.root, args.log)
            print(f"prospective recover={outcome}")
            return 0
        if args.cache is None or args.session is None or args.inception_session is None or args.as_of is None:
            print("a prospective day needs --cache, --session, --inception-session, and --as-of", file=sys.stderr)
            return 2
        if args.expect_hash is None or args.expect_records is None:
            print("a prospective day needs --expect-hash and --expect-records", file=sys.stderr)
            return 2
        result = run_prospective(
            root=args.root,
            log_path=args.log,
            cache=args.cache,
            session=date.fromisoformat(args.session),
            inception_session=date.fromisoformat(args.inception_session),
            as_of=datetime.fromisoformat(args.as_of),
            expect_hash=args.expect_hash,
            expect_records=args.expect_records,
            report=args.report,
            clock_mode=args.clock_mode,
        )
    except ResearchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"prospective status={result['status']} session={result['session']} "
        f"nav_gbp={result['nav_gbp']} mode={result['mode']} not_official_evidence=true"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

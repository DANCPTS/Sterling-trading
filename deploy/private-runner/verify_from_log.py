"""Recompute the hash chain and paper state from the log alone.

Does not trust the state file. OpenTimestamps stays off unless --stamp is passed,
and even then this stub does not contact a calendar server.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.portfolio import replay, state_hash
from sterling.research.grok.verify import verify_modes


def stamp_tip(digest: str, *, enabled: bool) -> str:
    if not enabled:
        return "opentimestamps=off"
    raise RuntimeError("OpenTimestamps stub is off. No calendar server was contacted.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a forward log from scratch.")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--stamp", action="store_true")
    args = parser.parse_args(argv)
    log = ProposalLog(args.log)
    count = log.verify()
    mode = verify_modes(log.records(), now=datetime.now(UTC))
    book = replay(log.records())
    print(f"ok records={count} mode={mode} state_hash={state_hash(book)} cash_gbp={book.cash}")
    print(stamp_tip(str(log.records()[-1].get("record_hash")), enabled=args.stamp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

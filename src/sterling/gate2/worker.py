"""Subprocess entry point for crash and race tests.

The worker prints one JSON decision and does not trade.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from sterling.gate2.admit import admit
from sterling.gate2.ingress import ingest
from sterling.gate2.models import AdmissionRequest, GenerationInput, TrustedClock
from sterling.gate2.service import _generation, _proposal
from sterling.gate2.store import open_store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("admit", "ingest"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--owner-anchor", type=Path, default=None)
    args = parser.parse_args()
    store = open_store(args.root, owner_anchor=args.owner_anchor)
    ready = os.environ.get("STERLING_GATE2_READY_FILE")
    start = os.environ.get("STERLING_GATE2_START_FILE")
    if ready is not None:
        Path(ready).write_text("ready", encoding="utf-8")
    if start is not None:
        while not Path(start).exists():
            time.sleep(0.01)
    document = json.loads(args.request.read_text(encoding="utf-8"))
    clock = TrustedClock(
        as_of=datetime.fromisoformat(str(document["clock"]["as_of"])),
        monotonic_ns=int(document["clock"]["monotonic_ns"]),
    )
    if args.action == "admit":
        result = admit(store, _as_request(document), clock)
    else:
        result = ingest(store, _as_generation(document), clock, peer_uid=int(document["peer_uid"]))
    print(
        json.dumps(
            {
                "outcome": result.outcome.value,
                "reasons": list(result.reasons),
                "reservation_id": result.reservation_id,
                "reservation_state": result.reservation_state,
                "replayed": result.replayed,
                "execution_permission_issued": False,
                "ticket_issued": False,
            }
        )
    )
    store.close()


def _as_request(document: dict[str, object]) -> AdmissionRequest:
    return _proposal(document)


def _as_generation(document: dict[str, object]) -> GenerationInput:
    return _generation(document)


if __name__ == "__main__":
    main()

"""Run a short-lived local Gate 2 coordinator. Not a deployed service."""

from __future__ import annotations

import argparse
from pathlib import Path

from sterling.gate2.service import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Gate 2 coordinator")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--proposal-socket", type=Path, required=True)
    parser.add_argument("--owner-socket", type=Path, required=True)
    parser.add_argument("--ingress-socket", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--owner-anchor", type=Path, default=None)
    args = parser.parse_args()
    serve(
        args.root,
        proposal_path=args.proposal_socket,
        owner_path=args.owner_socket,
        ingress_path=args.ingress_socket,
        stop_file=args.stop_file,
        owner_anchor=args.owner_anchor,
    )


if __name__ == "__main__":
    main()

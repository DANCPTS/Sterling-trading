"""Local read-only preview. Binds to loopback and does not fetch or trade.

Windows, from the repository root::

    py -m sterling.research.invest.preview --out dashboard.html

That writes a SYNTHETIC page. Open the file in a browser. To serve it:

    py -m sterling.research.invest.preview

Then open http://127.0.0.1:8765/ . The process listens on 127.0.0.1 only.

Import a verified private log without putting a token in the command. Clone
the private evidence branch with your existing Git credential, then:

    py -m sterling.research.invest.preview --import-log PATH\\evidence\\invest-v2\\mock\\forward.jsonl --out dashboard.html

The import runs the hash-chain check and the paper-v2 verifier. A failure
leaves an existing dashboard.html untouched.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from sterling.research.grok.providers import MockGrokProvider
from sterling.research.invest.book import Book
from sterling.research.invest.daily import run_daily
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.prompt import allocation_prompt
from sterling.research.invest.report import write_dashboard
from sterling.research.invest.schema import FUNDS

LONDON = ZoneInfo("Europe/London")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Sterling Invest preview. Loopback only.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out", type=Path, default=Path("dashboard.html"))
    parser.add_argument("--import-log", type=Path)
    parser.add_argument("--log", type=Path, help="Where the synthetic evidence file is written.")
    parser.add_argument("--no-serve", action="store_true")
    args = parser.parse_args(argv)
    if args.host not in _LOOPBACK:
        print("preview listens on 127.0.0.1 only", file=sys.stderr)
        return 2
    if args.import_log is not None:
        code = write_dashboard(args.import_log, args.out)
        if code != 0:
            print("import refused: evidence did not verify; last page kept", file=sys.stderr)
            return code
        print(f"verified page {args.out}")
    else:
        log_path = args.log or Path("synthetic-invest.jsonl")
        build_synthetic_log(log_path)
        if write_dashboard(log_path, args.out) != 0:
            print("synthetic page was not written", file=sys.stderr)
            return 1
        print(f"SYNTHETIC page {args.out}")
    if args.no_serve:
        return 0
    page = args.out.read_bytes()
    _serve(args.host, args.port, page)
    return 0


def build_synthetic_log(path: Path) -> None:
    """A short labelled fixture. Prices are inputs, not a published result."""
    saved = {key: os.environ.get(key) for key in ("STERLING_SYSTEM", "STERLING_EXPERIMENT", "STERLING_EVIDENCE_LANE")}
    os.environ["STERLING_SYSTEM"] = "sterling-invest"
    os.environ["STERLING_EXPERIMENT"] = "paper-v2"
    os.environ["STERLING_EVIDENCE_LANE"] = "mock"
    try:
        _write_synthetic(path)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_synthetic(path: Path) -> None:
    if path.exists():
        path.unlink()
    decision = datetime(2026, 11, 11, 18, 0, tzinfo=LONDON)
    prices = {name: Decimal("100") for name in FUNDS}
    prints = [(name, decision.date(), Decimal("100"), decision) for name in FUNDS]
    book = Book()
    prompt = allocation_prompt(book=book, prints=prints, as_of=decision, tradable=set(prices))
    run_invest_week(
        log_path=path,
        book=book,
        prices=prices,
        tradable=set(prices),
        as_of=decision,
        provider=MockGrokProvider(seed=1, response_format="allocation"),
        prompt=prompt,
        mode="simulated_clock",
    )
    sessions = [
        datetime(2026, 11, 12, 16, 30, tzinfo=LONDON),
        datetime(2026, 11, 13, 16, 30, tzinfo=LONDON),
        datetime(2026, 11, 16, 16, 30, tzinfo=LONDON),
        datetime(2026, 11, 17, 16, 30, tzinfo=LONDON),
        datetime(2026, 11, 18, 16, 30, tzinfo=LONDON),
    ]
    sonia = ((datetime(2026, 11, 10).date(), Decimal("4.00")),)
    history: dict[str, list[tuple[date, Decimal]]] = {name: [] for name in FUNDS}
    for index, moment in enumerate(sessions):
        opens = {name: Decimal("101") + index for name in FUNDS if name != "XSTR"}
        for name in FUNDS:
            history[name].append((moment.date(), Decimal("102") + index))
        closes = {name: tuple(points) for name, points in history.items()}
        series = ((moment.date(), Decimal("4.00")),) if index == len(sessions) - 1 else sonia
        run_daily(
            log_path=path,
            session=moment.date(),
            opens=opens,
            close_history=closes,
            sonia=series,
            recorded_at=moment,
            open_source="SYNTHETIC-open",
            data_label="SYNTHETIC",
        )


def _serve(host: str, port: int, page: bytes) -> None:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] not in {"/", "/index.html"}:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self) -> None:  # noqa: N802
            self.send_error(405)

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    bound = "127.0.0.1" if host == "localhost" else host
    server = ThreadingHTTPServer((bound, port), _Handler)
    print(f"listening on http://{bound}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())

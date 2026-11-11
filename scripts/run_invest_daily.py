"""One paper day. This process has no provider and does not call xAI."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sterling.research.invest.cash_rate import parse_sonia_csv
from sterling.research.invest.daily import OPEN_SOURCE_LIVE, run_daily
from sterling.research.invest.lanes import assert_lane
from sterling.research.invest.market import select_market
from sterling.research.invest.schema import FUNDS


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(item == "--provider" or item.startswith("--provider=") or item == "xai" for item in args):
        print("daily job does not call a provider", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Settle one paper-v2 day. No model call.")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--sonia-csv", type=Path)
    parser.add_argument("--label", default="live")
    parser.add_argument("--opens", default="")
    parser.add_argument("--closes", default="")
    parsed = parser.parse_args(args)
    lane = os.environ.get("STERLING_EVIDENCE_LANE", "").strip()
    if lane:
        try:
            assert_lane(parsed.log, lane)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    session = date.fromisoformat(parsed.session)
    as_of = datetime.now(UTC)
    sonia = parse_sonia_csv(parsed.sonia_csv.read_text(encoding="utf-8")) if parsed.sonia_csv else ()
    opens = _pairs(parsed.opens)
    history = {name: _series(parsed.closes, name) for name in FUNDS}
    notes = None
    if parsed.label == "live" and not opens:
        opens, history, sonia, notes = _live(session, as_of)
        source = OPEN_SOURCE_LIVE
    else:
        source = "SYNTHETIC-open" if parsed.label.upper() == "SYNTHETIC" else OPEN_SOURCE_LIVE
    result = run_daily(
        log_path=parsed.log,
        session=session,
        opens=opens,
        close_history=history,
        sonia=sonia,
        recorded_at=as_of,
        open_source=source,
        data_label=parsed.label,
        cross_check=notes,
    )
    print(f"daily status={result['status']} session={result['session']} provider_calls={result['provider_calls']}")
    return 0


def _pairs(text: str) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for item in text.split(","):
        if not item.strip():
            continue
        name, _, price = item.partition("=")
        found[name] = Decimal(price)
    return found


def _series(text: str, name: str) -> tuple[tuple[date, Decimal], ...]:
    rows: list[tuple[date, Decimal]] = []
    for item in text.split(","):
        if not item.startswith(name + "@"):
            continue
        _, _, rest = item.partition("@")
        day, _, price = rest.partition("=")
        rows.append((date.fromisoformat(day), Decimal(price)))
    return tuple(rows)


def _weekly():
    import importlib.util

    path = Path(__file__).resolve().parent / "run_weekly_decision.py"
    spec = importlib.util.spec_from_file_location("run_weekly_decision", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("price loader is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live(session: date, as_of: datetime):
    """Yahoo GBP closes and opens, plus published IUDSOIA. No synthetic fill-in."""
    module = _weekly()
    cache = Path("evidence")
    module.fetch_boe_sonia(cache / "boe_sonia.csv")
    sonia = parse_sonia_csv((cache / "boe_sonia.csv").read_text(encoding="utf-8"))
    primary, secondary, opens = module._live_inputs(cache)
    close_rows = []
    for item in primary:
        if item.instrument_id in FUNDS:
            close_rows.append((item.instrument_id, item.session, item.price, item.published_at))
    open_rows = []
    for ticker, price, day, at, source in opens:
        if source == "yahoo-open" and ticker in FUNDS:
            open_rows.append((ticker, date.fromisoformat(day), Decimal(price), datetime.fromisoformat(at)))
    second = []
    for item in secondary:
        if item.instrument_id in FUNDS:
            second.append((item.instrument_id, item.session, item.source))
    live_opens, history, notes = select_market(
        closes=close_rows,
        opens=open_rows,
        second_sources=second,
        session=session,
        as_of=as_of,
    )
    return live_opens, history, sonia, notes


if __name__ == "__main__":
    raise SystemExit(main())

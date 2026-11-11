"""One paper day. This process has no provider and does not call xAI."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sterling.research.invest.cash_rate import parse_sonia_csv
from sterling.research.invest.daily import OPEN_SOURCE_LIVE, run_daily
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
    session = date.fromisoformat(parsed.session)
    sonia = parse_sonia_csv(parsed.sonia_csv.read_text(encoding="utf-8")) if parsed.sonia_csv else ()
    opens = _pairs(parsed.opens)
    history = {name: _series(parsed.closes, name) for name in FUNDS}
    if parsed.label == "live" and not opens:
        opens, history = _live(session)
        source = OPEN_SOURCE_LIVE
    else:
        source = "SYNTHETIC-open" if parsed.label.upper() == "SYNTHETIC" else OPEN_SOURCE_LIVE
    result = run_daily(
        log_path=parsed.log,
        session=session,
        opens=opens,
        close_history=history,
        sonia=sonia,
        recorded_at=datetime.now(UTC),
        open_source=source,
        data_label=parsed.label,
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


def _live(session: date) -> tuple[dict[str, Decimal], dict[str, tuple[tuple[date, Decimal], ...]]]:
    """Yahoo daily opens and closes. A missing open is omitted, not replaced."""
    import importlib.util

    path = Path(__file__).resolve().parent / "run_weekly_decision.py"
    spec = importlib.util.spec_from_file_location("run_weekly_decision", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("price loader is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _primary, _secondary, opens = module._live_inputs(Path("evidence"))
    live_opens: dict[str, Decimal] = {}
    for ticker, price, day, _at, source in opens:
        if day == session.isoformat() and source == "yahoo-open":
            live_opens[ticker] = Decimal(price)
    history: dict[str, tuple[tuple[date, Decimal], ...]] = {name: () for name in FUNDS}
    return live_opens, history


if __name__ == "__main__":
    raise SystemExit(main())

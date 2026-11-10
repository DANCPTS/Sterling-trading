"""Run the synthetic illustration, or a local CSV folder. No network."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from sterling.research.calendar import xlon
from sterling.research.costs import CostModel
from sterling.research.engine import simulate
from sterling.research.instruments import load_candidate_universe
from sterling.research.loaders import LocalCsvSource
from sterling.research.report import format_report, write_report
from sterling.research.risk_config import load_research_limits
from sterling.research.strategies.trend import TrendFilter
from sterling.research.synthetic import ILLUSTRATION_END, ILLUSTRATION_START, run_illustration
from sterling.research.walkforward import walk_forward_splits

ROADMAP = (
    "Grok proposal source with forward-only timestamped evaluation (interface only; no model call).",
    "Individual-stock momentum, still research-only.",
    "FX research, still research-only and not an order path.",
    "Breakout and mean reversion.",
    "Swing research.",
)


def _print_splits() -> None:
    splits = walk_forward_splits(ILLUSTRATION_START, ILLUSTRATION_END, train_months=4, test_months=3)
    print("Walk-forward splits on the synthetic window (test starts the day after train ends):")
    if not splits:
        print("(none)")
        return
    for split in splits:
        print(
            f"  train {split.train_start.isoformat()}..{split.train_end.isoformat()} "
            f"test {split.test_start.isoformat()}..{split.test_end.isoformat()}"
        )
    last = splits[-1]
    out_of_sample = run_illustration(
        evaluate_start=last.test_start,
        evaluate_end=last.test_end,
        label="SYNTHETIC out-of-sample window — not an official result",
    )
    print()
    print(format_report(out_of_sample))


def run_csv(directory: Path, tickers: list[str], output: Path | None) -> None:
    universe = load_candidate_universe()
    source = LocalCsvSource(directory, universe)
    instruments = tuple(universe.by_ticker(ticker).as_instrument() for ticker in tickers)
    bars = {ticker: source.load(ticker) for ticker in tickers}
    limits = load_research_limits()
    result = simulate(
        instruments=instruments,
        bars=bars,
        calendar=xlon(),
        strategy=TrendFilter(lookback_months=10),
        limits=limits,
        cost_model=CostModel(commission_flat_gbp=Decimal("1.00"), spread_bps=Decimal("10"), slippage_bps=Decimal("5")),
        label="LOCAL CSV illustration — not an official performance result",
    )
    text = format_report(result, limit_lines=limits.summary_lines())
    print(text)
    if output is not None:
        write_report(result, output, limit_lines=limits.summary_lines())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Research-only simulator. No broker and no network.")
    parser.add_argument("--csv-dir", type=Path, help="Local folder of {ticker}.csv files. Not fetched.")
    parser.add_argument("--tickers", default="", help="Comma-separated tickers for --csv-dir.")
    parser.add_argument("--output", type=Path, help="Optional directory for CSV/JSON. Use a gitignored cache.")
    args = parser.parse_args(argv)
    if args.csv_dir is not None:
        tickers = [item.strip() for item in args.tickers.split(",") if item.strip()]
        if not tickers:
            raise SystemExit("Pass --tickers with --csv-dir.")
        run_csv(args.csv_dir, tickers, args.output)
        return
    limits = load_research_limits()
    universe = load_candidate_universe()
    result = run_illustration(limits=limits)
    print(
        "Draft universe (not frozen): starting set "
        + ", ".join(universe.provisional_starting_set)
        + ". Optional comparisons only: "
        + ", ".join(universe.optional_comparison_only)
        + "."
    )
    print("Calendar is XLON: England and Wales bank holidays, including Easter and proclaimed one-offs.")
    print()
    print(format_report(result, limit_lines=limits.summary_lines()))
    _print_splits()
    print("Roadmap (not implemented):")
    for item in ROADMAP:
        print(f"- {item}")
    if args.output is not None:
        write_report(result, args.output, limit_lines=limits.summary_lines())


if __name__ == "__main__":
    main()

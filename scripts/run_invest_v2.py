"""One Sterling Invest paper-v2 week. Mock by default. No scheduled run."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sterling.research.grok.providers import MockGrokProvider
from sterling.research.invest.adapter import LiveAllocationAdapter
from sterling.research.invest.book import Book
from sterling.research.invest.daily import load_book
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.lanes import assert_lane
from sterling.research.invest.prompt import allocation_prompt, latest_prices
from sterling.research.invest.recorded import RecordedAllocationProvider, load_recorded_response
from sterling.research.invest.schema import ALLOCATION_SCHEMA, FUNDS
from sterling.xai_client import XaiClient, format_dry_run_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append one paper-v2 weekly decision.")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--provider", choices=("mock", "xai", "recorded"), default="mock")
    parser.add_argument("--response-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--enable-xai", action="store_true")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--fetch-free", action="store_true")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--simulated-clock", action="store_true")
    parser.add_argument("--as-of", type=str)
    args = parser.parse_args(argv)
    lane = os.environ.get("STERLING_EVIDENCE_LANE", "").strip()
    if lane:
        try:
            assert_lane(args.log, lane)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if _repository_evidence(args.log):
        print("refusing to write a repository evidence log", file=sys.stderr)
        return 2
    os.environ.setdefault("STERLING_SYSTEM", "sterling-invest")
    os.environ.setdefault("STERLING_EXPERIMENT", "paper-v2")
    if args.provider == "recorded" and args.response_file is None:
        print("recorded provider needs --response-file", file=sys.stderr)
        return 2
    as_of = datetime.now(UTC) if args.as_of is None else datetime.fromisoformat(args.as_of)
    if args.dry_run or args.provider in {"mock", "recorded"} or args.fixture or args.as_of:
        prints = [(name, as_of.date(), Decimal("100"), as_of) for name in FUNDS]
        mode = "simulated_clock"
    else:
        prints = _live_prints(args.cache or Path("evidence"), as_of)
        mode = "forward"
    if args.dry_run:
        book = load_book(args.log) if args.log.is_file() else Book()
        prompt = allocation_prompt(book=book, prints=prints, as_of=as_of, tradable=set(latest_prices(prints, as_of)))
        report = XaiClient(enabled=False, dry_run=True, model="grok-4.3").dry_run_report(prompt, schema=ALLOCATION_SCHEMA)
        print(format_dry_run_report(report))
        return 0
    prices = latest_prices(prints, as_of)
    book = load_book(args.log) if args.log.is_file() else Book()
    prompt = allocation_prompt(book=book, prints=prints, as_of=as_of, tradable=set(prices))
    result = run_invest_week(
        log_path=args.log,
        book=book,
        prices=prices,
        tradable=set(prices),
        as_of=as_of,
        provider=_provider(args.provider, args.enable_xai, args.response_file),
        prompt=prompt,
        before_call=_publish_reserve,
        mode=mode,
    )
    print(
        f"policy=paper-v2 status={result['status']} week={result['week_key']} "
        f"provider_calls={result['provider_calls']} gate={result.get('gate', 'noop')} "
        f"execution_permission=NOT_ISSUED"
    )
    return 0


def _repository_evidence(path: Path) -> bool:
    root = Path(__file__).resolve().parents[1]
    banned = {
        (root / "evidence" / "forward.jsonl").resolve(),
        (root / "evidence" / "invest-v2" / "forward.jsonl").resolve(),
        (root / "evidence" / "invest-v2" / "mock" / "forward.jsonl").resolve(),
    }
    try:
        return path.resolve() in banned
    except OSError:
        return False


def _provider(name: str, enable_xai: bool, response_file: Path | None = None) -> object:
    if name == "mock":
        return MockGrokProvider(seed=1, response_format="allocation")
    if name == "recorded":
        if response_file is None:
            raise RuntimeError("recorded provider needs --response-file")
        return RecordedAllocationProvider(load_recorded_response(response_file), model_id="grok-4.3")
    return LiveAllocationAdapter(_weekly()._client(enable_xai, False))


def _weekly() -> object:
    path = Path(__file__).resolve().parent / "run_weekly_decision.py"
    spec = importlib.util.spec_from_file_location("run_weekly_decision", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("weekly price loader is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live_prints(cache: Path, as_of: datetime) -> list[tuple[str, datetime.date, Decimal, datetime]]:
    module = _weekly()
    primary, _secondary, _opens = module._live_inputs(cache)
    prints: list[tuple[str, datetime.date, Decimal, datetime]] = []
    for item in primary:
        if item.instrument_id in FUNDS and item.published_at <= as_of:
            prints.append((item.instrument_id, item.session, item.price, item.published_at))
    seen = {ticker for ticker, _session, _price, _at in prints}
    missing = sorted(set(FUNDS) - seen)
    if missing:
        print(f"missing cross-check or close for {','.join(missing)}", file=sys.stderr)
    return prints


def _publish_reserve() -> None:
    script = os.environ.get("STERLING_PUBLISH_PY", "").strip()
    clone = os.environ.get("STERLING_EVIDENCE_CLONE", "").strip()
    if not script or not clone:
        return
    import subprocess

    completed = subprocess.run(
        [sys.executable, script, "--clone", clone, "--branch", "evidence", "--remote", "origin"],
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("evidence reserve was not published")


if __name__ == "__main__":
    raise SystemExit(main())

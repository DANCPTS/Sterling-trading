"""One Sterling Invest paper-v2 week. Mock by default. No scheduled run."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sterling.research.grok.providers import MockGrokProvider, ProviderEnvelope
from sterling.research.invest.book import Book
from sterling.research.invest.daily import load_book
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.lanes import assert_lane
from sterling.research.invest.schema import ALLOCATION_SCHEMA, FUNDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append one paper-v2 weekly decision.")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--provider", choices=("mock", "xai"), default="mock")
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
    os.environ.setdefault("STERLING_SYSTEM", "sterling-invest")
    os.environ.setdefault("STERLING_EXPERIMENT", "paper-v2")
    as_of = datetime.now(UTC) if args.as_of is None else datetime.fromisoformat(args.as_of)
    if args.provider == "mock" or args.fixture or args.as_of:
        prices = {name: Decimal("100") for name in FUNDS}
        mode = "simulated_clock"
    else:
        prices = _live_prices(args.cache or Path("evidence"))
        mode = "forward"
    provider = _provider(args.provider, args.enable_xai)
    book = load_book(args.log) if args.log.is_file() else Book()
    result = run_invest_week(
        log_path=args.log,
        book=book,
        prices=prices,
        tradable=set(prices),
        as_of=as_of,
        provider=provider,
        prompt="paper-v2 allocation",
        before_call=_publish_reserve,
        mode=mode,
    )
    print(
        f"policy=paper-v2 status={result['status']} week={result['week_key']} "
        f"provider_calls={result['provider_calls']} gate={result.get('gate', 'noop')} "
        f"execution_permission=NOT_ISSUED"
    )
    return 0


def _provider(name: str, enable_xai: bool) -> object:
    if name == "mock":
        return MockGrokProvider(seed=1, response_format="allocation")
    client = _weekly()._client(enable_xai, False)

    class _Adapter:
        def complete(self, snapshot: object, *, prompt: str) -> ProviderEnvelope:
            del snapshot
            content, _usage = client.complete(prompt, schema=ALLOCATION_SCHEMA)
            return ProviderEnvelope(model_id=client.model, responded_at=datetime.now(UTC), raw_response=content)

    return _Adapter()


def _weekly() -> object:
    path = Path(__file__).resolve().parent / "run_weekly_decision.py"
    spec = importlib.util.spec_from_file_location("run_weekly_decision", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("weekly price loader is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live_prices(cache: Path) -> dict[str, Decimal]:
    module = _weekly()
    primary, _secondary, _opens = module._live_inputs(cache)
    latest: dict[str, Decimal] = {}
    for item in primary:
        if item.instrument_id in FUNDS:
            latest[item.instrument_id] = item.price
    if len(latest) < len(FUNDS):
        missing = sorted(set(FUNDS) - set(latest))
        print(f"missing cross-check or close for {','.join(missing)}", file=sys.stderr)
    return latest


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

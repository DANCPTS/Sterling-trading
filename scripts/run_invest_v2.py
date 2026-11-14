"""One Sterling Invest paper-v2 week. Mock by default. No scheduled run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sterling.research.grok.providers import MockGrokProvider
from sterling.research.invest.adapter import LiveAllocationAdapter
from sterling.research.invest.book import Book
from sterling.research.invest.cash_rate import parse_sonia_csv
from sterling.research.invest.daily import load_book
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.lanes import assert_lane
from sterling.research.invest.prompt import PROMPT_VERSION, allocation_prompt, latest_prices
from sterling.research.invest.recorded import RecordedAllocationProvider, load_recorded_response
from sterling.research.invest.schema import FUNDS, allocation_schema
from sterling.research.invest.snapshot import build_invest_snapshot, missing_names, stale_names, tradable_names
from sterling.xai_client import DEFAULT_MODEL, XaiClient, format_dry_run_report


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
    if args.dry_run and args.fetch_free:
        print("dry-run does not fetch and does not open a connection", file=sys.stderr)
        return 2
    if args.dry_run and not args.fixture and args.cache is None:
        print("dry-run needs --fixture or --cache with --as-of", file=sys.stderr)
        return 2
    if args.dry_run and args.cache is not None and not args.fixture and args.as_of is None:
        print("a cache dry-run needs --as-of", file=sys.stderr)
        return 2
    try:
        prints, provenance = _observations(args, as_of)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    mode = "simulated_clock" if provenance == "synthetic-fixture" or args.as_of or args.fixture else "forward"
    book = load_book(args.log) if args.log.is_file() else Book()
    prices = latest_prices(prints, as_of)
    sonia, sonia_reason = _sonia(args)
    research = build_invest_snapshot(
        book=book,
        prints=prints,
        as_of=as_of,
        requested_tradable=set(prices) if prices else set(FUNDS),
        sonia=sonia,
        provenance=provenance,
        sonia_absence_reason=sonia_reason,
    )
    prompt = allocation_prompt(
        book=book,
        prints=prints,
        as_of=as_of,
        tradable=set(prices),
        sonia=sonia,
        snapshot=research,
        provenance=provenance,
    )
    if args.provider == "xai" and provenance == "synthetic-fixture":
        print("refusing a paid xAI call on synthetic-fixture prices. No request was sent.", file=sys.stderr)
        return 2
    if args.dry_run:
        schema = allocation_schema(DEFAULT_MODEL)
        report = XaiClient(enabled=False, dry_run=True, model=DEFAULT_MODEL).dry_run_report(prompt, schema=schema)
        report["data_source"] = provenance
        report["missing"] = ",".join(sorted(missing_names(research))) or "none"
        report["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        report["prompt_version"] = PROMPT_VERSION
        availability = research.get("data_availability")
        published = ""
        if isinstance(availability, dict):
            published = str(availability.get("publication") or "")
        report["publication"] = published
        report["schema_sha256"] = hashlib.sha256(
            json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        report["stale"] = ",".join(sorted(stale_names(research))) or "none"
        report["tradable"] = ",".join(sorted(tradable_names(research))) or "none"
        sonia_block = research.get("sonia")
        if isinstance(sonia_block, dict):
            report["sonia_status"] = str(sonia_block.get("status"))
            report["sonia_reason"] = str(sonia_block.get("reason") or "")
            report["sonia_reference_date"] = str(sonia_block.get("reference_date") or "")
            report["sonia_percent"] = str(sonia_block.get("sonia_percent") or "")
            report["sonia_annual_decimal"] = str(sonia_block.get("annual_decimal") or "")
            report["sonia_published_at"] = str(sonia_block.get("published_at") or "")
        print(format_dry_run_report(report))
        return 0
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
        prints=prints,
        research_snapshot=research,
    )
    print(
        f"policy=paper-v2 status={result['status']} week={result['week_key']} "
        f"provider_calls={result['provider_calls']} gate={result.get('gate', 'noop')} "
        f"execution_permission=NOT_ISSUED"
    )
    return 0


def _observations(args: argparse.Namespace, as_of: datetime) -> tuple[list[tuple[str, date, Decimal, datetime]], str]:
    if args.cache is not None and not args.fixture and not args.fetch_free:
        return _cache_prints(args.cache, as_of), "supplied"
    if args.dry_run or args.provider in {"mock", "recorded"} or args.fixture or args.as_of:
        return [(name, as_of.date(), Decimal("100"), as_of) for name in FUNDS], "synthetic-fixture"
    return _live_prints(args.cache or Path("evidence"), as_of), "supplied"


def _sonia(args: argparse.Namespace) -> tuple[tuple[tuple[date, Decimal], ...], str | None]:
    if args.cache is None or args.fixture:
        return (), None
    path = args.cache / "boe_sonia.csv"
    if not path.is_file():
        return (), "sonia_cache_missing"
    try:
        rows = parse_sonia_csv(path.read_text(encoding="utf-8"))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f"boe_sonia.csv is not a readable IUDSOIA file: {exc}") from exc
    if not rows:
        return (), "sonia_cache_empty"
    return rows, None


def _cache_prints(cache: Path, as_of: datetime) -> list[object]:
    """Local CSV rows. Availability metadata is required and is never invented."""
    from sterling.research.invest.availability import (
        BASIS_ASSUMED,
        BASIS_VERIFIED,
        assumed_available_at,
        exchange_close_at,
    )
    from sterling.research.invest.snapshot import PricePrint

    prints: list[PricePrint] = []
    for ticker in FUNDS:
        path = cache / f"{ticker}.csv"
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", newline="") as handle:
            for record in csv.DictReader(handle):
                prints.append(
                    _one_cached_print(
                        ticker,
                        record,
                        as_of,
                        assumed_available_at,
                        exchange_close_at,
                        BASIS_VERIFIED,
                        BASIS_ASSUMED,
                        PricePrint,
                    )
                )
    kept = [item for item in prints if item.available_at <= as_of]
    if not kept:
        raise ValueError("cache has no price available at or before as-of")
    return kept


def _one_cached_print(
    ticker: str,
    record: dict[str, str],
    as_of: datetime,
    assumed_available_at: object,
    exchange_close_at: object,
    verified: str,
    assumed: str,
    price_print: type,
) -> object:
    del as_of
    session = date.fromisoformat(record["date"][:10])
    price = Decimal(record["close"])
    basis = (record.get("availability_basis") or "").strip()
    verified_text = (record.get("provider_published_at") or record.get("published_at") or "").strip()
    if basis == assumed:
        available = assumed_available_at(session)  # type: ignore[operator]
        provider_time = _aware(verified_text, f"{ticker}.csv provider_published_at") if verified_text else None
        basis_name = assumed
    elif basis in {"", verified} and verified_text:
        provider_time = _aware(verified_text, f"{ticker}.csv published_at")
        available = provider_time
        basis_name = verified
    else:
        raise ValueError(f"{ticker}.csv needs provider_published_at or availability_basis=assumed-next-session")
    close_text = (record.get("exchange_close_at") or "").strip()
    close_at = _aware(close_text, f"{ticker}.csv exchange_close_at") if close_text else exchange_close_at(session)  # type: ignore[operator]
    retrieved_text = (record.get("retrieved_at") or "").strip()
    retrieved = _aware(retrieved_text, f"{ticker}.csv retrieved_at") if retrieved_text else None
    return price_print(
        ticker=ticker,
        session=session,
        price=price,
        available_at=available,
        exchange_close_at=close_at,
        provider_published_at=provider_time,
        retrieved_at=retrieved,
        availability_basis=basis_name,
        source=(record.get("source") or "local-csv").strip() or "local-csv",
        quoted_currency=(record.get("quoted_currency") or "").strip() or None,
        gbp_divisor=(record.get("gbp_divisor") or "").strip() or None,
    )


def _aware(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} needs a timezone")
    return parsed


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

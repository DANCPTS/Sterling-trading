"""Weekly forward decision. Mock provider by default. No scheduled workflow.

``--fetch-free`` reads Yahoo and Stooq through ``urllib`` into the cache process
and does not write the raw bodies. Stooq is ignored until ``stooq-units.csv``
names the currency. A single local CSV is not a fresh price: the name is HOLD.
``--demo-offline`` uses a synthetic book and does not open a socket.
``--healthcheck`` only verifies an existing log.
"""

from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sterling.marketdata.free_prices import STOOQ_SYMBOLS, YAHOO_SYMBOLS, fetch_bytes, stooq_csv, yahoo_chart
from sterling.research.calendar import xlon_weekday
from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.compare import synthetic_bars
from sterling.research.grok.context import PriceObservation, SoniaObservation, build_market_context
from sterling.research.grok.preregistration import APPROVED_UNIVERSE, PILOT_LABEL
from sterling.research.grok.providers import MockGrokProvider
from sterling.research.grok.quotes import assess_instrument, gbp_divisor, to_gbp
from sterling.research.grok.schema import InstrumentQuote, MarketSnapshot, estimate_tokens, prompt_for
from sterling.research.grok.timekeeping import SystemClock, TickingClock
from sterling.research.grok.verify import verify_modes
from sterling.research.grok.weekly import WeekResult, record_unquoted_week, run_week, state_file
from sterling.xai_client import UsageRecord, XaiClient, XaiError

BOE_SONIA = (
    "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
    "?csv.x=yes&Datefrom=01/Jan/2024&Dateto=now&SeriesCodes=IUDSOIA&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append one weekly forward decision, or verify a log.")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--provider", choices=("mock", "xai"), default="mock")
    parser.add_argument("--enable-xai", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--fetch-free", action="store_true")
    parser.add_argument("--demo-offline", action="store_true")
    parser.add_argument("--fixture", action="store_true", help="Labelled fixture prices and an injected clock.")
    parser.add_argument("--simulated-clock", action="store_true")
    parser.add_argument("--as-of", type=str)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.healthcheck:
            records = healthcheck(args.log)
            print(f"ok records={records} phase={PILOT_LABEL}")
            return 0
        if (args.demo_offline or args.fixture or args.simulated_clock) and not args.as_of:
            raise ResearchError("demo and simulated-clock modes need --as-of so they are not recorded as today")
        as_of = datetime.now(UTC) if args.as_of is None else _as_of(args.as_of)
        prints = fixture_prints(as_of) if args.fixture or args.simulated_clock else None
        holds: dict[str, str] = {}
        if args.fixture or args.simulated_clock:
            snapshot = fixture_snapshot(as_of, prints or [])
            mode = "simulated_clock"
        else:
            snapshot, holds, prints = build_snapshot(
                as_of=as_of,
                cache=args.cache,
                fetch_free=args.fetch_free,
                demo_offline=args.demo_offline,
            )
            mode = "demo" if args.demo_offline else "forward"
        if snapshot is None:
            result = record_unquoted_week(
                log_path=args.log,
                state_path=state_file(args.log),
                mode=mode,
                hold_reasons=holds,
                clock=TickingClock(as_of) if mode != "forward" else SystemClock(),
                recorder=SystemClock(),
            )
            print(
                f"phase={PILOT_LABEL} status={result.status} week={result.week_key} "
                f"provider_calls={result.provider_calls} gate={result.gate} state_hash={result.state_hash}"
            )
            return 0
        result, usage = run_decision(
            log_path=args.log,
            snapshot=snapshot,
            provider_name=args.provider,
            enable_xai=args.enable_xai,
            dry_run=args.dry_run,
            mode=mode,
            as_of=as_of if mode != "forward" else None,
            prints=prints,
            hold_reasons=holds,
        )
        print(
            f"phase={PILOT_LABEL} status={result.status} week={result.week_key} "
            f"provider_calls={result.provider_calls} gate={result.gate} "
            f"state_hash={result.state_hash} token_estimate={estimate_tokens(prompt_for(snapshot))} "
            f"provider={args.provider} execution_permission=NOT_ISSUED"
        )
        if usage is not None:
            print(f"http_status={usage.http_status} request_hash={usage.request_hash}")
        return 0
    except (OSError, ResearchError, XaiError, ValueError) as exc:
        print(f"forward decision failed: {exc}", file=sys.stderr)
        usage = getattr(exc, "usage", None)
        if isinstance(usage, UsageRecord):
            _append_usage(args.log, usage, when=_as_of(args.as_of) if args.as_of else datetime.now(UTC))
        return 1


def healthcheck(path: Path) -> int:
    if not path.is_file():
        raise ResearchError("forward log does not exist")
    log = ProposalLog(path)
    count = log.verify()
    if any(record.get("mode") for record in log.records()):
        verify_modes(log.records(), now=datetime.now(UTC))
    return count


def build_snapshot(
    *,
    as_of: datetime,
    cache: Path | None,
    fetch_free: bool,
    demo_offline: bool,
) -> tuple[MarketSnapshot | None, dict[str, str], list[PriceObservation] | None]:
    if demo_offline:
        return _demo_snapshot(as_of), {}, None
    if cache is None:
        raise ResearchError("pass --cache with local CSVs, or --demo-offline. No prices were fetched.")
    if fetch_free:
        fetch_boe_sonia(cache / "boe_sonia.csv")
        primary, secondary = _download_free(cache)
    else:
        primary = _prices_from_cache(cache, as_of, suffix=".csv", source="local-csv")
        secondary = _prices_from_cache(cache, as_of, suffix=".check.csv", source="local-check")
    sonia = _sonia_from_cache(cache / "boe_sonia.csv", as_of) if (cache / "boe_sonia.csv").is_file() else ()
    return _assessed(as_of, primary, secondary, sonia)


def run_decision(
    *,
    log_path: Path,
    snapshot: MarketSnapshot,
    provider_name: str,
    enable_xai: bool,
    dry_run: bool,
    client: XaiClient | None = None,
    mode: str = "forward",
    as_of: datetime | None = None,
    prints: list[PriceObservation] | None = None,
    hold_reasons: dict[str, str] | None = None,
) -> tuple[WeekResult, UsageRecord | None]:
    usage_box: dict[str, UsageRecord | None] = {"usage": None}

    def _provide(current: MarketSnapshot, prompt: str) -> tuple[str, str]:
        del current
        if provider_name == "mock":
            envelope = MockGrokProvider(seed=1).complete(snapshot, prompt=prompt)
            return envelope.raw_response, envelope.model_id
        xai = client or XaiClient(enabled=enable_xai, dry_run=dry_run)
        content, usage = xai.complete(prompt)
        usage_box["usage"] = usage
        _append_usage(log_path, usage, when=datetime.now(UTC))
        return content, xai.model

    clock = TickingClock(as_of) if mode != "forward" and as_of is not None else SystemClock()
    result = run_week(
        log_path=log_path,
        state_path=state_file(log_path),
        snapshot=snapshot,
        provider=_provide,
        mode=mode,
        clock=clock,
        recorder=SystemClock(),
        prints=prints,
        hold_reasons=hold_reasons,
    )
    if provider_name == "xai" and result.status == "decided" and result.gate != "accepted":
        raise ResearchError(f"xAI proposal was rejected: {result.gate}")
    return result, usage_box["usage"]


def fetch_boe_sonia(destination: Path) -> None:
    """Free Bank of England file. No API key. Not used by the default mock path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(BOE_SONIA, headers={"User-Agent": "SterlingResearch/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        destination.write_bytes(response.read())


def _open_log(path: Path, created: datetime) -> ProposalLog:
    log = ProposalLog(path)
    if not path.exists():
        log.create(now=created)
    return log


def _append_usage(path: Path, usage: UsageRecord, *, when: datetime) -> None:
    if not path.exists():
        return
    log = ProposalLog(path)
    try:
        log.append_event(timestamp_utc=when, record_type="model_usage", fields=usage.as_log())
    except ResearchError:
        return


def fixture_prints(as_of: datetime) -> list[PriceObservation]:
    """Synthetic GBP prints labelled fixture. Not a market feed and not fresh live data."""
    day = as_of.date() - timedelta(days=80)
    rows: list[PriceObservation] = []
    price = Decimal("100")
    while day <= as_of.date():
        if day.weekday() < 5:
            published = datetime(day.year, day.month, day.day, 16, 30, tzinfo=UTC)
            if published <= as_of:
                for ticker in APPROVED_UNIVERSE:
                    rows.append(PriceObservation(ticker, day, price, published, "fixture"))
            price += Decimal("0.05")
        day += timedelta(days=1)
    return rows


def fixture_snapshot(as_of: datetime, prints: list[PriceObservation]) -> MarketSnapshot:
    latest: dict[str, PriceObservation] = {}
    for item in prints:
        if item.published_at <= as_of:
            latest[item.instrument_id] = item
    quotes = tuple(
        InstrumentQuote(ticker, latest[ticker].price, latest[ticker].published_at, "fixture") for ticker in APPROVED_UNIVERSE
    )
    context = build_market_context(as_of=as_of, observations=prints)
    return MarketSnapshot(
        as_of=as_of,
        quotes=quotes,
        positions=(),
        nav_gbp=Decimal("10000"),
        universe=APPROVED_UNIVERSE,
        features=context.features,
        distributions=context.distributions,
        cash_rate=None,
    )


def _demo_snapshot(as_of: datetime) -> MarketSnapshot:
    sessions = xlon_weekday().trading_days(date(2024, 5, 1), date(2024, 6, 7))
    bars = synthetic_bars(sessions, APPROVED_UNIVERSE, seed=1)
    observations = [
        PriceObservation(
            ticker, point.session, point.close, datetime.combine(point.session, datetime.min.time(), tzinfo=UTC), "synthetic"
        )
        for ticker, series in bars.items()
        for point in series
        if datetime.combine(point.session, datetime.min.time(), tzinfo=UTC) <= as_of
    ]
    context = build_market_context(as_of=as_of, observations=observations)
    quotes = []
    for ticker in APPROVED_UNIVERSE:
        last = [item for item in observations if item.instrument_id == ticker][-1]
        quotes.append(InstrumentQuote(ticker, last.price, last.published_at, "synthetic"))
    return MarketSnapshot(
        as_of=as_of,
        quotes=tuple(quotes),
        positions=(),
        nav_gbp=Decimal("10000"),
        universe=APPROVED_UNIVERSE,
        features=context.features,
        distributions=(),
        cash_rate=None,
    )


def _prices_from_cache(cache: Path, as_of: datetime, *, suffix: str, source: str) -> list[PriceObservation]:
    rows: list[PriceObservation] = []
    for ticker in APPROVED_UNIVERSE:
        path = cache / f"{ticker}{suffix}"
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", newline="") as handle:
            for record in csv.DictReader(handle):
                session = date.fromisoformat(record["date"][:10])
                published = datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(hour=16, minute=30)
                if published > as_of:
                    continue
                quoted = record.get("quoted_in") or "GBP"
                divisor = gbp_divisor(ticker) if quoted in {"GBp", "GBX"} else Decimal(1)
                price = to_gbp(Decimal(record["close"]), quoted, divisor)
                rows.append(PriceObservation(ticker, session, price, published, source))
    return rows


def _download_free(cache: Path) -> tuple[list[PriceObservation], list[PriceObservation]]:
    """Fetch Yahoo and Stooq. Raw bodies are not written. Stooq needs stooq-units.csv."""
    units = _stooq_units(cache / "stooq-units.csv")

    def _getter(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "SterlingResearch/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return bytes(response.read())

    primary: list[PriceObservation] = []
    secondary: list[PriceObservation] = []
    for ticker, symbol in YAHOO_SYMBOLS.items():
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2y"
        primary.extend(_gbp_prints(yahoo_chart(ticker, fetch_bytes(url, _getter)), "yahoo-chart"))
    for ticker, symbol in STOOQ_SYMBOLS.items():
        unit = units.get(ticker)
        if unit is None:
            continue
        url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
        secondary.extend(_gbp_prints(stooq_csv(ticker, fetch_bytes(url, _getter), quoted_in=unit), "stooq"))
    return primary, secondary


def _stooq_units(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    units: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            units[record["ticker"]] = record["quoted_in"]
    return units


def _gbp_prints(prints: tuple[object, ...], source: str) -> list[PriceObservation]:
    rows: list[PriceObservation] = []
    for item in prints:
        quoted = str(item.quoted_in)
        if quoted == "UNVERIFIED":
            continue
        ticker = str(item.ticker)
        divisor = gbp_divisor(ticker) if quoted in {"GBp", "GBX"} else Decimal(1)
        try:
            price = to_gbp(Decimal(str(item.price)), quoted, divisor)
        except ResearchError:
            continue
        session = item.session
        published = item.published_at
        rows.append(PriceObservation(ticker, session, price, published, source))
    return rows


def _assessed(
    as_of: datetime,
    primary: list[PriceObservation],
    secondary: list[PriceObservation],
    sonia: tuple[SoniaObservation, ...],
) -> tuple[MarketSnapshot | None, dict[str, str], list[PriceObservation]]:
    moments = sorted({item.published_at for item in (*primary, *secondary)})
    holds: dict[str, str] = {}
    quotes: list[InstrumentQuote] = []
    kept: list[PriceObservation] = []
    for ticker in APPROVED_UNIVERSE:
        verdict = assess_instrument(
            ticker,
            primary,
            secondary,
            as_of=as_of,
            sessions=moments,
            max_age_sessions=5,
        )
        if verdict.action != "USE" or verdict.price is None or verdict.published_at is None:
            holds[ticker] = verdict.reason
            continue
        quotes.append(InstrumentQuote(ticker, verdict.price, verdict.published_at, verdict.source))
        kept.extend(item for item in primary if item.instrument_id == ticker and item.published_at <= as_of)
    if not quotes:
        return None, holds, kept
    context = build_market_context(as_of=as_of, observations=kept, sonia=sonia)
    snapshot = MarketSnapshot(
        as_of=as_of,
        quotes=tuple(quotes),
        positions=(),
        nav_gbp=Decimal("10000"),
        universe=APPROVED_UNIVERSE,
        features=context.features,
        distributions=context.distributions,
        cash_rate=context.cash_rate,
    )
    return snapshot, holds, kept


def _sonia_from_cache(path: Path, as_of: datetime) -> tuple[SoniaObservation, ...]:
    rows: list[SoniaObservation] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            observed = date.fromisoformat(record["date"][:10])
            published = datetime.combine(observed + timedelta(days=1), datetime.min.time(), tzinfo=UTC).replace(hour=8)
            if published > as_of:
                continue
            percent = Decimal(record["rate"])
            rows.append(SoniaObservation(observed, percent / Decimal(100), published, "boe-iadb"))
    return tuple(rows)


def _as_of(text: str | None) -> datetime:
    if text is None:
        return datetime(2024, 6, 7, 16, 30, tzinfo=UTC)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ResearchError("as-of must be UTC")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())

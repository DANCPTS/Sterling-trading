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
import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sterling.marketdata.free_prices import (
    ISHARES_PORTFOLIOS,
    YAHOO_SYMBOLS,
    boe_columns,
    fetch_bytes,
    first_seen_many,
    ishares_nav_xls,
    usd_on_valuation_date,
    vanguard_nav_html,
    yahoo_bars,
)
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
from sterling.xai_client import API_URL, HttpResponse, UsageRecord, XaiClient, XaiError

QUOTE_NOTES: list[str] = []
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
        opens: dict[str, tuple[str, str, str, str]] = {}
        if args.fixture or args.simulated_clock:
            snapshot = fixture_snapshot(as_of, prints or [])
            mode = "simulated_clock"
            opens = _choose_opens(_fixture_opens(prints or []), _pending_decision(args.log), as_of)
        else:
            snapshot, holds, prints, open_rows = build_snapshot(
                as_of=as_of,
                cache=args.cache,
                fetch_free=args.fetch_free,
                demo_offline=args.demo_offline,
            )
            mode = "demo" if args.demo_offline else "forward"
            opens = _choose_opens(open_rows, _pending_decision(args.log), as_of)
            for note in QUOTE_NOTES:
                print(note)
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
            opens=opens,
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


class _OfflineXai:
    """Test transport. It does not open a socket."""

    def post(self, *, url: str, body: bytes, headers: object, connect_timeout: float, read_timeout: float) -> HttpResponse:
        del body, headers, connect_timeout, read_timeout
        if url != API_URL:
            raise XaiError("offline xAI transport refuses any other URL")
        content = json.dumps(
            {
                "model_id": "grok-4.3",
                "proposals": [
                    {
                        "action": "BUY",
                        "confidence": "0.35",
                        "instrument_id": ticker,
                        "reasoning": "offline fixture",
                        "target_weight": "0.16666666",
                    }
                    for ticker in ("VWRP", "CUKX", "IGLT", "AGBP", "SGLN", "XSTR")
                ],
            }
        )
        payload = json.dumps(
            {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "cost_in_usd": "0.00000000"},
            }
        )
        return HttpResponse(status=200, body=payload.encode("utf-8"))


def _client(enable_xai: bool, dry_run: bool) -> XaiClient:
    if os.environ.get("STERLING_XAI_OFFLINE") == "1":
        return XaiClient(enabled=enable_xai, dry_run=dry_run, transport=_OfflineXai(), sleeper=lambda _delay: None)
    return XaiClient(enabled=enable_xai, dry_run=dry_run)


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
) -> tuple[MarketSnapshot | None, dict[str, str], list[PriceObservation] | None, list[tuple[str, str, str, str, str]]]:
    if demo_offline:
        return _demo_snapshot(as_of), {}, None, []
    if cache is None:
        raise ResearchError("pass --cache with local CSVs, or --demo-offline. No prices were fetched.")
    open_rows: list[tuple[str, str, str, str, str]] = []
    if fetch_free:
        fetch_boe_sonia(cache / "boe_sonia.csv")
        primary, secondary, open_rows = _live_inputs(cache)
        as_of = datetime.now(UTC)
    else:
        primary = _prices_from_cache(cache, as_of, suffix=".csv", source="local-csv")
        secondary = _prices_from_cache(cache, as_of, suffix=".check.csv", source="local-check")
    sonia = _sonia_from_cache(cache / "boe_sonia.csv", as_of) if (cache / "boe_sonia.csv").is_file() else ()
    snapshot, holds, kept = _assessed(as_of, primary, secondary, sonia)
    return snapshot, holds, kept, open_rows


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
    opens: dict[str, tuple[str, str, str, str]] | None = None,
) -> tuple[WeekResult, UsageRecord | None]:
    usage_box: dict[str, UsageRecord | None] = {"usage": None}

    def _provide(current: MarketSnapshot, prompt: str) -> tuple[str, str]:
        del current
        if provider_name == "mock":
            envelope = MockGrokProvider(seed=1).complete(snapshot, prompt=prompt)
            return envelope.raw_response, envelope.model_id
        xai = client or _client(enable_xai, dry_run)
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
        opens=opens,
        before_call=_publish_reserve,
    )
    return result, usage_box["usage"]


def _publish_reserve() -> None:
    script = os.environ.get("STERLING_PUBLISH_PY", "").strip()
    clone = os.environ.get("STERLING_EVIDENCE_CLONE", "").strip()
    if not script or not clone:
        return
    completed = subprocess.run(
        [sys.executable, script, "--clone", clone, "--branch", "evidence", "--remote", "origin"],
        check=False,
    )
    if completed.returncode != 0:
        raise ResearchError("evidence reserve was not published")


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


def _live_inputs(
    cache: Path,
) -> tuple[list[PriceObservation], list[PriceObservation], list[tuple[str, str, str, str, str]]]:
    """Yahoo bars plus issuer or independent second prints. Raw bodies are not stored."""

    def _getter(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return bytes(response.read())

    boe_url = (
        "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
        "?csv.x=yes&Datefrom=01/Jan/2024&Dateto=now&SeriesCodes=XUDLUSS,XUDLERS"
        "&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"
    )
    now = datetime.now(UTC)
    rates = boe_columns(fetch_bytes(boe_url, _getter))
    fx = rates.get("XUDLUSS", {})
    eur = rates.get("XUDLERS", {})
    closes: list[PriceObservation] = []
    navs: list[PriceObservation] = []
    opens: list[tuple[str, str, str, str, str]] = []
    close_units: dict[str, str] = {}
    nav_units: dict[str, str] = {}
    for ticker, symbol in YAHOO_SYMBOLS.items():
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2y"
        for bar in yahoo_bars(ticker, fetch_bytes(url, _getter)):
            if bar.close_price is not None and bar.close_at <= now:
                closes.append(_gbp_observation(ticker, bar.session, bar.close_price, bar.quoted_in, bar.close_at, "yahoo-chart"))
                close_units[ticker] = bar.quoted_in
            if bar.open_price is not None and bar.open_at <= now:
                price = _gbp_amount(ticker, bar.open_price, bar.quoted_in)
                if price is not None:
                    opens.append((ticker, format(price, "f"), bar.session.isoformat(), bar.open_at.isoformat(), "yahoo-open"))
    for ticker, portfolio in ISHARES_PORTFOLIOS.items():
        url = (
            "https://www.ishares.com/varnish-api/uk-retail01-product-data/product-data/api/v1/get-fund-document"
            "?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=ishares-uk&locale=en_GB"
            f"&portfolioId={portfolio}&component=fundDownloadV2&userType=individual"
        )
        rows = []
        for item in ishares_nav_xls(ticker, fetch_bytes(url, _getter)):
            price = _nav_gbp(item.price, item.quoted_in, item.valuation_date, fx)
            if price is None:
                continue
            rows.append((item, price))
        seen = first_seen_many(
            cache / "first_observed.json",
            [f"ishares-nav|{ticker}|{item.valuation_date.isoformat()}|{item.price}" for item, _price in rows],
            datetime.now(UTC),
        )
        for item, price in rows:
            published = seen[f"ishares-nav|{ticker}|{item.valuation_date.isoformat()}|{item.price}"]
            navs.append(PriceObservation(ticker, item.valuation_date, price, published, "ishares-nav"))
            nav_units[ticker] = item.quoted_in
    vanguard_url = "https://www.vanguard.co.uk/professional/product/etf/equity/9679/ftse-all-world-ucits-etf-usd-accumulating"
    for item in vanguard_nav_html("VWRP", fetch_bytes(vanguard_url, _getter)):
        price = usd_on_valuation_date(item.price, item.valuation_date, fx)
        if price is None:
            continue
        identity = f"vanguard-nav|VWRP|{item.valuation_date.isoformat()}|{item.price}"
        published = first_seen_many(cache / "first_observed.json", [identity], datetime.now(UTC))[identity]
        navs.append(PriceObservation("VWRP", item.valuation_date, price, published, "vanguard-nav"))
        nav_units["VWRP"] = item.quoted_in
    xetra_url = "https://query1.finance.yahoo.com/v8/finance/chart/DXS1.DE?interval=1d&range=2y"
    xetra_rows = []
    for bar in yahoo_bars("XSTR", fetch_bytes(xetra_url, _getter)):
        if bar.quoted_in != "EUR" or bar.close_price is None or bar.close_at > now:
            continue
        price = usd_on_valuation_date(bar.close_price, bar.session, eur)
        if price is None:
            continue
        xetra_rows.append((bar, price))
    xetra_seen = first_seen_many(
        cache / "first_observed.json",
        [f"yahoo-xetra-eur|XSTR|{bar.session.isoformat()}|{price}" for bar, price in xetra_rows],
        datetime.now(UTC),
    )
    for bar, price in xetra_rows:
        identity = f"yahoo-xetra-eur|XSTR|{bar.session.isoformat()}|{price}"
        navs.append(PriceObservation("XSTR", bar.session, price, xetra_seen[identity], "yahoo-xetra-eur"))
        nav_units["XSTR"] = "EUR"
    _remember_notes(closes, navs, close_units, nav_units)
    return closes, navs, opens


def _remember_notes(
    closes: list[PriceObservation],
    navs: list[PriceObservation],
    close_units: dict[str, str],
    nav_units: dict[str, str],
) -> None:
    QUOTE_NOTES.clear()
    for ticker in YAHOO_SYMBOLS:
        close = [item for item in closes if item.instrument_id == ticker]
        nav = [item for item in navs if item.instrument_id == ticker]
        last_close = max(close, key=lambda item: item.session) if close else None
        last_nav = max(nav, key=lambda item: item.session) if nav else None
        shared = sorted({item.session for item in close} & {item.session for item in nav})
        paired = ""
        if shared:
            day = shared[-1]
            close_price = next(item.price for item in close if item.session == day)
            nav_price = next(item.price for item in nav if item.session == day)
            gap = (close_price - nav_price) / nav_price
            paired = f" paired_session={day.isoformat()} paired_close_gbp={close_price} premium={gap}"
        QUOTE_NOTES.append(
            " ".join(
                [
                    ticker,
                    f"close_gbp={last_close.price if last_close else 'none'}",
                    f"close_session={last_close.session.isoformat() if last_close else 'none'}",
                    f"close_published={last_close.published_at.isoformat() if last_close else 'none'}",
                    f"close_unit={close_units.get(ticker, 'none')}",
                    f"close_source={last_close.source if last_close else 'none'}",
                    f"nav_gbp={last_nav.price if last_nav else 'none'}",
                    f"nav_unit={nav_units.get(ticker, 'none')}",
                    f"nav_valuation={last_nav.session.isoformat() if last_nav else 'none'}",
                    f"nav_published={last_nav.published_at.isoformat() if last_nav else 'none'}",
                    f"nav_source={last_nav.source if last_nav else 'none'}{paired}",
                ]
            )
        )


def _pending_decision(path: Path) -> datetime | None:
    if not path.is_file():
        return None
    latest: datetime | None = None
    for record in ProposalLog(path).records():
        if record.get("record_type") != "decision":
            continue
        text = record.get("decision_at")
        if isinstance(text, str):
            latest = datetime.fromisoformat(text)
    return latest


def _choose_opens(
    rows: list[tuple[str, str, str, str, str]],
    pending: datetime | None,
    as_of: datetime,
) -> dict[str, tuple[str, str, str, str]]:
    chosen: dict[str, tuple[str, str, str, str]] = {}
    if pending is None:
        return chosen
    for ticker, price, session, available, source in rows:
        moment = datetime.fromisoformat(available)
        if moment <= pending or moment > as_of:
            continue
        current = chosen.get(ticker)
        if current is None or available < current[2]:
            chosen[ticker] = (price, session, available, source)
    return chosen


def _fixture_opens(prints: list[PriceObservation]) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for item in prints:
        available = datetime(item.session.year, item.session.month, item.session.day, 8, 0, tzinfo=UTC)
        rows.append((item.instrument_id, format(item.price, "f"), item.session.isoformat(), available.isoformat(), "fixture-open"))
    return rows


def _gbp_amount(ticker: str, price: Decimal, quoted: str) -> Decimal | None:
    divisor = gbp_divisor(ticker) if quoted in {"GBp", "GBX"} else Decimal(1)
    try:
        converted = to_gbp(price, "GBP" if quoted == "GBP" else quoted, divisor)
        return converted.quantize(Decimal("0.000001"))
    except ResearchError:
        return None


def _gbp_observation(
    ticker: str, session: date, price: Decimal, quoted: str, published: datetime, source: str
) -> PriceObservation | None:
    amount = _gbp_amount(ticker, price, quoted)
    if amount is None:
        raise ResearchError(f"{ticker} quote unit {quoted} was not converted")
    return PriceObservation(ticker, session, amount, published, source)


def _nav_gbp(price: Decimal, quoted: str, valuation: date, fx: dict[date, Decimal]) -> Decimal | None:
    if quoted == "GBP":
        return price
    if quoted == "USD":
        return usd_on_valuation_date(price, valuation, fx)
    return _gbp_amount("SGLN", price, quoted)


def _assessed(
    as_of: datetime,
    primary: list[PriceObservation],
    secondary: list[PriceObservation],
    sonia: tuple[SoniaObservation, ...],
) -> tuple[MarketSnapshot | None, dict[str, str], list[PriceObservation]]:
    moments = sorted({item.published_at for item in primary})
    holds: dict[str, str] = {}
    quotes: list[InstrumentQuote] = []
    kept: list[PriceObservation] = []
    for ticker in APPROVED_UNIVERSE:
        xetra = any(item.source == "yahoo-xetra-eur" for item in secondary if item.instrument_id == ticker)
        comparison = "xetra_fx" if xetra else "nav"
        verdict = assess_instrument(
            ticker,
            primary,
            secondary,
            as_of=as_of,
            sessions=moments,
            max_age_sessions=5,
            comparison=comparison,
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
            raw_date = record.get("date") or record.get("DATE") or ""
            raw_rate = record.get("rate") or record.get("IUDSOIA")
            if not raw_date or not raw_rate:
                continue
            try:
                observed = date.fromisoformat(raw_date[:10])
            except ValueError:
                observed = datetime.strptime(raw_date.strip(), "%d %b %Y").date()
            published = datetime.combine(observed + timedelta(days=1), datetime.min.time(), tzinfo=UTC).replace(hour=8)
            if published > as_of:
                continue
            percent = Decimal(raw_rate)
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

"""Weekly forward decision. Mock provider by default. No scheduled workflow.

``--fetch-free`` downloads only the Bank of England IADB file into the cache.
ETF prices stay in local CSVs. ``--demo-offline`` uses a synthetic book and
does not open a socket. ``--healthcheck`` only verifies an existing log.
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

from sterling.research.calendar import xlon_weekday
from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.compare import synthetic_bars
from sterling.research.grok.context import PriceObservation, SoniaObservation, build_market_context
from sterling.research.grok.preregistration import APPROVED_UNIVERSE, PILOT_LABEL
from sterling.research.grok.providers import MockGrokProvider, ProviderEnvelope
from sterling.research.grok.schema import AccountView, InstrumentQuote, MarketSnapshot, estimate_tokens, prompt_for
from sterling.research.grok.session import ForwardSession
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
    parser.add_argument("--as-of", type=str)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.healthcheck:
            records = healthcheck(args.log)
            print(f"ok records={records} phase={PILOT_LABEL}")
            return 0
        as_of = _as_of(args.as_of)
        snapshot = build_snapshot(
            as_of=as_of,
            cache=args.cache,
            fetch_free=args.fetch_free,
            demo_offline=args.demo_offline,
        )
        usage = run_decision(
            log_path=args.log,
            snapshot=snapshot,
            provider_name=args.provider,
            enable_xai=args.enable_xai,
            dry_run=args.dry_run,
        )
        print(
            f"phase={PILOT_LABEL} token_estimate={estimate_tokens(prompt_for(snapshot))} "
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
    return ProposalLog(path).verify()


def build_snapshot(*, as_of: datetime, cache: Path | None, fetch_free: bool, demo_offline: bool) -> MarketSnapshot:
    if demo_offline:
        return _demo_snapshot(as_of)
    if cache is None:
        raise ResearchError("pass --cache with local CSVs, or --demo-offline. No prices were fetched.")
    if fetch_free:
        fetch_boe_sonia(cache / "boe_sonia.csv")
    observations = _prices_from_cache(cache, as_of)
    sonia = _sonia_from_cache(cache / "boe_sonia.csv", as_of) if (cache / "boe_sonia.csv").is_file() else ()
    context = build_market_context(as_of=as_of, observations=observations, sonia=sonia)
    quotes = []
    for ticker in APPROVED_UNIVERSE:
        latest = [item for item in observations if item.instrument_id == ticker]
        if not latest:
            raise ResearchError(f"{ticker} has no price published at or before the decision")
        quotes.append(InstrumentQuote(ticker, latest[-1].price, latest[-1].published_at, latest[-1].source))
    return MarketSnapshot(
        as_of=as_of,
        quotes=tuple(quotes),
        positions=(),
        nav_gbp=Decimal("10000"),
        universe=APPROVED_UNIVERSE,
        features=context.features,
        distributions=context.distributions,
        cash_rate=context.cash_rate,
    )


def run_decision(
    *,
    log_path: Path,
    snapshot: MarketSnapshot,
    provider_name: str,
    enable_xai: bool,
    dry_run: bool,
    client: XaiClient | None = None,
) -> UsageRecord | None:
    log = _open_log(log_path, snapshot.as_of - timedelta(days=1))
    usage: UsageRecord | None = None
    if provider_name == "mock":
        provider = MockGrokProvider(seed=1)
    else:
        xai = client or XaiClient(enabled=enable_xai, dry_run=dry_run)
        content, usage = xai.complete(prompt_for(snapshot))
        provider = _Ready(ProviderEnvelope(model_id=xai.model, responded_at=snapshot.as_of, raw_response=content))
    session = ForwardSession(log, provider)
    decision = session.decide(
        snapshot,
        received_at=snapshot.as_of + timedelta(seconds=1),
        account=AccountView(Decimal("500.00"), Decimal("500.00"), Decimal("0.00")),
        paper_nav_gbp=snapshot.nav_gbp,
    )
    if decision.execution_permission != "NOT_ISSUED" or decision.orders_submitted != 0:
        raise ResearchError("weekly decision lost the execution DENY")
    if usage is not None:
        _append_usage(log_path, usage, when=decision.screen_logged_at + timedelta(microseconds=1))
    if log.verify() < 2:
        raise ResearchError("forward log did not record the decision")
    if provider_name == "xai" and not decision.accepted_for_paper:
        raise ResearchError("xAI proposal was rejected: " + ";".join(decision.reasons))
    return usage


class _Ready:
    def __init__(self, envelope: ProviderEnvelope) -> None:
        self._envelope = envelope

    def complete(self, snapshot: MarketSnapshot, *, prompt: str) -> ProviderEnvelope:
        del snapshot, prompt
        return self._envelope


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


def _prices_from_cache(cache: Path, as_of: datetime) -> list[PriceObservation]:
    rows: list[PriceObservation] = []
    for ticker in APPROVED_UNIVERSE:
        path = cache / f"{ticker}.csv"
        if not path.is_file():
            raise ResearchError(f"{ticker} CSV is missing from the cache")
        with path.open(encoding="utf-8", newline="") as handle:
            for record in csv.DictReader(handle):
                session = date.fromisoformat(record["date"][:10])
                published = datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(hour=16, minute=30)
                if published > as_of:
                    continue
                rows.append(PriceObservation(ticker, session, Decimal(record["close"]), published, "local-csv"))
    return rows


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

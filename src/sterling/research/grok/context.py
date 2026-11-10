"""Point-in-time features for the six-ETF forward snapshot.

A print is usable only when its publication time is at or before the decision.
SONIA is the caller's lagged series: this module does not invent a publication
time and it does not fetch a rate. Missing history becomes a null feature so
the prompt shape stays fixed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import require_utc
from sterling.research.grok.preregistration import APPROVED_UNIVERSE
from sterling.research.grok.schema import CashRate, DistributionNote, MarketFeature
from sterling.research.metrics import TRADING_DAYS, sample_stdev
from sterling.research.money import ZERO

WINDOWS = {"1w": 5, "1m": 21, "3m": 63, "6m": 126, "12m": 252}
BENCHMARK_WINDOWS = ("1m", "3m", "12m")
VOL_SESSIONS = 63
DRAWDOWN_SESSIONS = 252
TREND_MONTHS = 10
RECENT_DISTRIBUTIONS = 3
_QUANTUM = Decimal("0.00000001")
SIXTY_FORTY = (
    ("VWRP", Decimal("0.30")),
    ("CUKX", Decimal("0.30")),
    ("IGLT", Decimal("0.20")),
    ("AGBP", Decimal("0.20")),
)


@dataclass(frozen=True, slots=True)
class PriceObservation:
    instrument_id: str
    session: date
    price: Decimal
    published_at: datetime
    source: str = "close"


@dataclass(frozen=True, slots=True)
class SoniaObservation:
    observed_on: date
    annual_rate: Decimal
    published_at: datetime
    source: str = "boe-iadb"


@dataclass(frozen=True, slots=True)
class DistributionObservation:
    instrument_id: str
    ex_date: date
    pay_date: date | None
    amount: Decimal
    published_at: datetime
    source: str = "issuer"


@dataclass(frozen=True, slots=True)
class MarketContext:
    features: tuple[MarketFeature, ...]
    distributions: tuple[DistributionNote, ...]
    cash_rate: CashRate | None


def build_market_context(
    *,
    as_of: datetime,
    observations: Sequence[PriceObservation],
    sonia: Sequence[SoniaObservation] = (),
    distributions: Sequence[DistributionObservation] = (),
) -> MarketContext:
    """Features for ``APPROVED_UNIVERSE`` using prints published at or before ``as_of``."""
    stamped = require_utc(as_of)
    _reject_foreign(observations, distributions)
    series = {ticker: _usable(observations, ticker, stamped) for ticker in APPROVED_UNIVERSE}
    features: list[MarketFeature] = []
    by_name: dict[str, MarketFeature] = {}
    for ticker in APPROVED_UNIVERSE:
        points = series[ticker]
        for label, sessions in WINDOWS.items():
            feature = _return_feature(ticker, label, sessions, points, stamped)
            features.append(feature)
            by_name[feature.name] = feature
        features.append(_trend(ticker, points, stamped))
        features.append(_volatility(ticker, points, stamped))
        current, worst = _drawdowns(ticker, points, stamped)
        features.append(current)
        features.append(worst)
    for label in BENCHMARK_WINDOWS:
        features.append(_basket(f"bench.equal_weight.return_{label}", APPROVED_UNIVERSE, None, by_name, label, stamped))
        names = tuple(name for name, _weight in SIXTY_FORTY)
        weights = {name: weight for name, weight in SIXTY_FORTY}
        features.append(_basket(f"bench.sixty_forty.return_{label}", names, weights, by_name, label, stamped))
    return MarketContext(
        features=tuple(sorted(features, key=lambda item: item.name)),
        distributions=_recent_distributions(distributions, stamped),
        cash_rate=_sonia(sonia, stamped),
    )


def _reject_foreign(
    observations: Sequence[PriceObservation],
    distributions: Sequence[DistributionObservation],
) -> None:
    allowed = set(APPROVED_UNIVERSE)
    foreign = sorted({item.instrument_id for item in observations if item.instrument_id not in allowed})
    foreign.extend(sorted({item.instrument_id for item in distributions if item.instrument_id not in allowed}))
    if foreign:
        raise ResearchError("market context is limited to the six-ETF universe: " + ", ".join(foreign))


def _usable(observations: Sequence[PriceObservation], ticker: str, as_of: datetime) -> tuple[PriceObservation, ...]:
    chosen: dict[date, PriceObservation] = {}
    for item in observations:
        if item.instrument_id != ticker or item.price <= 0:
            if item.instrument_id == ticker and item.price <= 0:
                raise ResearchError(f"{ticker} price must be positive")
            continue
        published = require_utc(item.published_at)
        if published > as_of:
            continue
        current = chosen.get(item.session)
        if current is None or published >= require_utc(current.published_at):
            chosen[item.session] = item
    return tuple(chosen[day] for day in sorted(chosen))


def _feature(
    name: str,
    value: Decimal | None,
    as_of: datetime,
    sources: Sequence[datetime],
    instrument_id: str | None,
) -> MarketFeature:
    cleaned = tuple(sorted(require_utc(moment) for moment in sources))
    for moment in cleaned:
        if moment > as_of:
            raise ResearchError("feature source is after the as-of time")
    quantized = None if value is None else value.quantize(_QUANTUM)
    return MarketFeature(name=name, value=quantized, as_of=as_of, sources=cleaned, instrument_id=instrument_id)


def _return_feature(
    ticker: str,
    label: str,
    sessions: int,
    points: Sequence[PriceObservation],
    as_of: datetime,
) -> MarketFeature:
    name = f"{ticker}.return_{label}"
    if len(points) <= sessions:
        return _feature(name, None, as_of, (), ticker)
    latest = points[-1]
    earlier = points[-1 - sessions]
    if earlier.price <= 0:
        return _feature(name, None, as_of, (), ticker)
    value = latest.price / earlier.price - 1
    return _feature(name, value, as_of, (earlier.published_at, latest.published_at), ticker)


def _trend(ticker: str, points: Sequence[PriceObservation], as_of: datetime) -> MarketFeature:
    name = f"{ticker}.trend_vs_10m"
    ends = _month_ends(points)
    if len(ends) < TREND_MONTHS or not points:
        return _feature(name, None, as_of, (), ticker)
    window = ends[-TREND_MONTHS:]
    average = sum((item.price for item in window), ZERO) / Decimal(TREND_MONTHS)
    if average <= 0:
        return _feature(name, None, as_of, (), ticker)
    value = points[-1].price / average - 1
    sources = [item.published_at for item in window]
    if points[-1].published_at not in sources:
        sources.append(points[-1].published_at)
    return _feature(name, value, as_of, sources, ticker)


def _month_ends(points: Sequence[PriceObservation]) -> tuple[PriceObservation, ...]:
    last: dict[tuple[int, int], PriceObservation] = {}
    for item in points:
        last[(item.session.year, item.session.month)] = item
    return tuple(last[key] for key in sorted(last))


def _volatility(ticker: str, points: Sequence[PriceObservation], as_of: datetime) -> MarketFeature:
    name = f"{ticker}.realised_vol_63d"
    needed = VOL_SESSIONS + 1
    if len(points) < needed:
        return _feature(name, None, as_of, (), ticker)
    window = points[-needed:]
    returns = [window[index].price / window[index - 1].price - 1 for index in range(1, len(window))]
    deviation = sample_stdev(returns)
    value = deviation * TRADING_DAYS.sqrt()
    return _feature(name, value, as_of, [item.published_at for item in window], ticker)


def _drawdowns(ticker: str, points: Sequence[PriceObservation], as_of: datetime) -> tuple[MarketFeature, MarketFeature]:
    current_name = f"{ticker}.drawdown"
    worst_name = f"{ticker}.max_drawdown_252d"
    if len(points) < 2:
        empty = _feature(current_name, None, as_of, (), ticker)
        return empty, _feature(worst_name, None, as_of, (), ticker)
    window = points[-DRAWDOWN_SESSIONS:]
    peak = window[0].price
    worst = ZERO
    for item in window:
        if item.price > peak:
            peak = item.price
        if peak > 0:
            drawdown = item.price / peak - 1
            if drawdown < worst:
                worst = drawdown
    current = window[-1].price / peak - 1 if peak > 0 else None
    sources = [item.published_at for item in window]
    worst_value = worst if len(points) >= DRAWDOWN_SESSIONS else None
    worst_sources = sources if worst_value is not None else ()
    return (
        _feature(current_name, current, as_of, sources, ticker),
        _feature(worst_name, worst_value, as_of, worst_sources, ticker),
    )


def _basket(
    name: str,
    tickers: Sequence[str],
    weights: Mapping[str, Decimal] | None,
    features: Mapping[str, MarketFeature],
    label: str,
    as_of: datetime,
) -> MarketFeature:
    parts: list[Decimal] = []
    sources: list[datetime] = []
    for ticker in tickers:
        feature = features[f"{ticker}.return_{label}"]
        if feature.value is None:
            return _feature(name, None, as_of, (), None)
        weight = Decimal(1) / Decimal(len(tickers)) if weights is None else weights[ticker]
        parts.append(weight * feature.value)
        sources.extend(feature.sources)
    return _feature(name, sum(parts, ZERO), as_of, sources, None)


def _recent_distributions(
    distributions: Sequence[DistributionObservation],
    as_of: datetime,
) -> tuple[DistributionNote, ...]:
    usable = [
        item
        for item in distributions
        if require_utc(item.published_at) <= as_of and item.amount >= 0 and item.instrument_id in APPROVED_UNIVERSE
    ]
    usable.sort(key=lambda item: (item.instrument_id, item.published_at, item.ex_date))
    kept: dict[str, list[DistributionObservation]] = {}
    for item in usable:
        kept.setdefault(item.instrument_id, []).append(item)
    notes: list[DistributionNote] = []
    for ticker in sorted(kept):
        for item in kept[ticker][-RECENT_DISTRIBUTIONS:]:
            notes.append(
                DistributionNote(
                    instrument_id=item.instrument_id,
                    ex_date=item.ex_date,
                    pay_date=item.pay_date,
                    amount=item.amount,
                    published_at=require_utc(item.published_at),
                    source=item.source,
                )
            )
    return tuple(notes)


def _sonia(observations: Sequence[SoniaObservation], as_of: datetime) -> CashRate | None:
    usable = [item for item in observations if require_utc(item.published_at) <= as_of]
    if not usable:
        return None
    chosen = max(usable, key=lambda item: (require_utc(item.published_at), item.observed_on))
    return CashRate(
        series_id="SONIA",
        annual_rate=chosen.annual_rate,
        observed_on=chosen.observed_on,
        published_at=require_utc(chosen.published_at),
        as_of=as_of,
        source=chosen.source,
    )

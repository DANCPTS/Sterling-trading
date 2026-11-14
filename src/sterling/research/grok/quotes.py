"""Normalise free prices. This module does not open a socket.

A missing, stale, or disagreeing print becomes HOLD for that name. The old
print is not copied forward and labelled as fresh. Yahoo rows are not
redistributable; callers keep them in a gitignored cache.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import require_utc
from sterling.research.grok.context import PriceObservation
from sterling.research.grok.preregistration import APPROVED_UNIVERSE
from sterling.research.instruments import load_candidate_universe

# A flat 2% band would treat a normal ETF premium, including XSTR's roughly
# 1.6% March 2020 discount, as a broken price. The check is the fund's own
# premium history: HOLD when the latest premium is at least 1 percentage
# point and at least 3 standard deviations from the prior sample, or when
# the gap exceeds 10%, which is a unit or file error rather than a premium.
# With fewer than 60 overlapping sessions the 10% cap still applies, and a
# gap above 5% is also a HOLD. A smaller gap is kept and labelled as a short
# sample, not as a match within 2%.
MIN_PREMIUM_PAIRS = 60
PREMIUM_Z = Decimal("3")
MATERIAL_PREMIUM = Decimal("0.01")
MARKET_GAP = Decimal("0.01")
# Xetra closes at 16:30 London and the Bank of England euro fixing is earlier
# the same afternoon. Half an hour of EUR/GBP movement is normally a few
# basis points. Half a percent leaves room for a rough afternoon without
# accepting a missed currency conversion.
FX_TIMING_GAP = Decimal("0.005")
UNIT_ERROR_PREMIUM = Decimal("0.10")
SHORT_HISTORY_PREMIUM = Decimal("0.05")


@dataclass(frozen=True, slots=True)
class CorporateAction:
    instrument_id: str
    session: datetime
    ratio: Decimal
    published_at: datetime
    kind: str


@dataclass(frozen=True, slots=True)
class QuoteVerdict:
    instrument_id: str
    action: str
    price: Decimal | None
    published_at: datetime | None
    source: str
    reason: str


# Yahoo's chart JSON stores closes as binary floats. Four decimal places in GBP
# keeps a 0.01 pence LSE quote (GBp / 100) and drops the extra float digits.
YAHOO_GBP_QUANTUM = Decimal("0.0001")


def normalise_yahoo_gbp(price: Decimal) -> Decimal:
    """Round a Yahoo daily close to 0.0001 GBP, half up."""
    if price <= 0:
        raise ResearchError("Yahoo GBP close must be positive")
    return price.quantize(YAHOO_GBP_QUANTUM, rounding=ROUND_HALF_UP)


def gbp_divisor(ticker: str) -> Decimal:
    for item in load_candidate_universe().instruments:
        if item.ticker == ticker:
            return item.price_divisor_to_gbp
    raise ResearchError(f"{ticker} is outside the research universe")


def to_gbp(price: Decimal, quoted_in: str, divisor: Decimal) -> Decimal:
    if price <= 0 or divisor <= 0:
        raise ResearchError("price and divisor must be positive")
    if quoted_in in {"GBp", "GBX"}:
        return price / divisor
    if quoted_in == "GBP" and divisor == 1:
        return price
    if quoted_in == "GBP":
        raise ResearchError("GBP quote must not also carry a pence divisor")
    raise ResearchError(f"unsupported quote unit {quoted_in}")


def adjust_for_actions(
    observations: Sequence[PriceObservation],
    actions: Sequence[CorporateAction],
) -> tuple[PriceObservation, ...]:
    """Scale prints published before a split or consolidation. Ratio is new/old."""
    out: list[PriceObservation] = []
    for item in observations:
        scale = Decimal(1)
        published = require_utc(item.published_at)
        for action in actions:
            if action.instrument_id != item.instrument_id or action.ratio <= 0:
                if action.instrument_id == item.instrument_id and action.ratio <= 0:
                    raise ResearchError("corporate-action ratio must be positive")
                continue
            if published < require_utc(action.published_at):
                scale *= action.ratio
        price = item.price / scale
        out.append(PriceObservation(item.instrument_id, item.session, price, item.published_at, item.source))
    return tuple(out)


def assess_instrument(
    instrument_id: str,
    primary: Sequence[PriceObservation],
    secondary: Sequence[PriceObservation],
    *,
    as_of: datetime,
    sessions: Sequence[datetime],
    max_age_sessions: int,
    comparison: str = "nav",
) -> QuoteVerdict:
    """HOLD when the latest agreed print is missing, stale, or the sources disagree."""
    if instrument_id not in APPROVED_UNIVERSE:
        return QuoteVerdict(instrument_id, "HOLD", None, None, "none", "outside_universe")
    stamped = require_utc(as_of)
    first = _latest(primary, instrument_id, stamped)
    second = _latest(secondary, instrument_id, stamped)
    if first is None or second is None:
        return QuoteVerdict(instrument_id, "HOLD", None, None, "none", "missing_or_not_cross_checked")
    if _stale(first.published_at, stamped, sessions, max_age_sessions) or _stale(
        second.published_at, stamped, sessions, max_age_sessions
    ):
        return QuoteVerdict(instrument_id, "HOLD", None, None, first.source, "stale")
    aligned = _aligned(primary, secondary, instrument_id, stamped)
    prices = [(item.price, nav.price) for item, nav in aligned]
    if comparison == "xetra_fx":
        status, reason = _fx_timing_reason(prices)
    elif comparison == "market":
        status, reason = _market_reason(prices)
    else:
        status, reason = premium_reason(prices)
    if status != "USE" or not aligned:
        return QuoteVerdict(instrument_id, "HOLD", None, None, "premium", reason)
    chosen, _nav = aligned[-1]
    if _stale(chosen.published_at, stamped, sessions, max_age_sessions):
        return QuoteVerdict(instrument_id, "HOLD", None, None, chosen.source, "stale")
    return QuoteVerdict(instrument_id, "USE", chosen.price, chosen.published_at, chosen.source, reason)


def _fx_timing_reason(pairs: Sequence[tuple[Decimal, Decimal]]) -> tuple[str, str]:
    """LSE sterling close against an Xetra euro close converted on that date."""
    if not pairs:
        return "HOLD", "missing_or_not_cross_checked"
    close, other = pairs[-1]
    if other <= 0 or abs(close - other) / other > FX_TIMING_GAP:
        return "HOLD", "fx_timing_gap"
    return "USE", "cross_checked_xetra"


def _market_reason(pairs: Sequence[tuple[Decimal, Decimal]]) -> tuple[str, str]:
    """Two market prints. A gap above 1% is a venue or unit error, not an NAV premium."""
    if not pairs:
        return "HOLD", "missing_or_not_cross_checked"
    close, other = pairs[-1]
    if other <= 0 or abs(close - other) / other > MARKET_GAP:
        return "HOLD", "market_prices_disagree"
    return "USE", "cross_checked_market"


def premium_reason(pairs: Sequence[tuple[Decimal, Decimal]]) -> tuple[str, str]:
    """Return USE or HOLD and a reason. ``pairs`` are (exchange_close, issuer_nav) in GBP."""
    if not pairs:
        return "HOLD", "missing_or_not_cross_checked"
    close, nav = pairs[-1]
    if nav <= 0 or close <= 0:
        return "HOLD", "premium_anomaly"
    latest = abs(close - nav) / nav
    if latest > UNIT_ERROR_PREMIUM:
        return "HOLD", "premium_anomaly"
    prior = pairs[:-1]
    if len(prior) < MIN_PREMIUM_PAIRS:
        if latest > SHORT_HISTORY_PREMIUM:
            return "HOLD", "premium_anomaly"
        return "USE", "cross_checked_short_history"
    gaps = [(item_close - item_nav) / item_nav for item_close, item_nav in prior if item_nav > 0]
    if len(gaps) < MIN_PREMIUM_PAIRS:
        return "HOLD", "premium_anomaly"
    mean = sum(gaps, Decimal(0)) / Decimal(len(gaps))
    variance = sum((item - mean) ** 2 for item in gaps) / Decimal(len(gaps))
    scale = variance.sqrt()
    signed = (close - nav) / nav
    if scale > 0 and abs(signed - mean) >= PREMIUM_Z * scale and abs(signed) >= MATERIAL_PREMIUM:
        return "HOLD", "premium_anomaly"
    return "USE", "cross_checked"


def _aligned(
    primary: Sequence[PriceObservation],
    secondary: Sequence[PriceObservation],
    instrument_id: str,
    as_of: datetime,
) -> list[tuple[PriceObservation, PriceObservation]]:
    firsts: dict[date, PriceObservation] = {}
    seconds: dict[date, PriceObservation] = {}
    for item in primary:
        if item.instrument_id == instrument_id and item.price > 0 and require_utc(item.published_at) <= as_of:
            firsts[item.session] = item
    for item in secondary:
        if item.instrument_id == instrument_id and item.price > 0 and require_utc(item.published_at) <= as_of:
            seconds[item.session] = item
    pairs: list[tuple[PriceObservation, PriceObservation]] = []
    for session in sorted(set(firsts) & set(seconds)):
        pairs.append((firsts[session], seconds[session]))
    return pairs


def _latest(
    observations: Sequence[PriceObservation],
    instrument_id: str,
    as_of: datetime,
) -> PriceObservation | None:
    chosen: PriceObservation | None = None
    for item in observations:
        if item.instrument_id != instrument_id or item.price <= 0:
            continue
        published = require_utc(item.published_at)
        if published > as_of:
            continue
        if (
            chosen is None
            or published > require_utc(chosen.published_at)
            or (published == require_utc(chosen.published_at) and item.session >= chosen.session)
        ):
            chosen = item
    return chosen


def _stale(published: datetime, as_of: datetime, sessions: Sequence[datetime], max_age: int) -> bool:
    if max_age < 1:
        raise ResearchError("max quote age must be at least one session")
    usable = [require_utc(moment) for moment in sessions if require_utc(moment) <= as_of]
    if not usable:
        return True
    recent = usable[-max_age:]
    return require_utc(published) < recent[0]


def divisors() -> Mapping[str, Decimal]:
    return {ticker: gbp_divisor(ticker) for ticker in APPROVED_UNIVERSE}

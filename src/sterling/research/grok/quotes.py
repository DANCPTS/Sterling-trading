"""Normalise free prices. This module does not open a socket.

A missing, stale, or disagreeing print becomes HOLD for that name. The old
print is not copied forward and labelled as fresh. Yahoo rows are not
redistributable; callers keep them in a gitignored cache.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import require_utc
from sterling.research.grok.context import PriceObservation
from sterling.research.grok.preregistration import APPROVED_UNIVERSE
from sterling.research.instruments import load_candidate_universe

DISAGREEMENT = Decimal("0.02")


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
    gap = abs(first.price - second.price) / first.price
    if gap > DISAGREEMENT:
        return QuoteVerdict(instrument_id, "HOLD", None, None, "disagreement", "sources_disagree")
    return QuoteVerdict(instrument_id, "USE", first.price, first.published_at, first.source, "cross_checked")


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
        if chosen is None or published >= require_utc(chosen.published_at):
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

"""Proxy splicing and currency conversion for research series.

These helpers do not download anything. A spliced series is scaled so the
proxy meets the actual series on the splice date. Observations after the
splice come only from the actual series.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sterling.research.errors import ResearchError
from sterling.research.money import ZERO

Point = tuple[date, Decimal]
Labelled = tuple[date, Decimal, str]


def drop_isolated_spikes(levels: Sequence[Point], limit: Decimal = Decimal("0.25")) -> tuple[Point, ...]:
    """Drop a print that jumps and fully reverses on the next observation.

    Yahoo's SGLN history in spring 2011 alternates between about £18 and £30.
    A move that is still there the next day is kept. This is vendor cleaning,
    not a trading rule.
    """
    ordered = sorted(levels, key=lambda item: item[0])
    if len(ordered) < 3:
        return tuple(ordered)
    kept = [ordered[0]]
    for index in range(1, len(ordered) - 1):
        previous = kept[-1][1]
        current = ordered[index][1]
        nxt = ordered[index + 1][1]
        if previous > 0 and abs(current / previous - 1) > limit and abs(nxt / previous - 1) <= limit:
            continue
        kept.append(ordered[index])
    kept.append(ordered[-1])
    return tuple(kept)


def distribution_total_return(
    observations: Sequence[tuple[date, Decimal, Decimal]],
) -> tuple[Point, ...]:
    """Reinvest a dividend on the session that carries it.

    ``observations`` are ``(session, close, dividend)`` in one currency.
    The index follows ``(close + dividend) / previous close``. Putting the
    dividend on the next session leaves a one-day hole the size of the
    distribution. That hole is not a fall in the cash index.
    """
    if not observations:
        return ()
    ordered = sorted(observations, key=lambda item: item[0])
    level = ordered[0][1]
    if level <= 0:
        raise ResearchError("distribution total return needs a positive first close")
    out = [(ordered[0][0], level)]
    previous_close = level
    for day, close, dividend in ordered[1:]:
        if close <= 0 or dividend < 0:
            raise ResearchError(f"distribution total return has a bad print on {day.isoformat()}")
        level *= (close + dividend) / previous_close
        out.append((day, level))
        previous_close = close
    return tuple(out)


def usd_price_to_gbp(price_usd: Decimal, usd_per_gbp: Decimal) -> Decimal:
    """Convert a US-dollar price using Bank of England dollars-per-pound.

    ``usd_per_gbp`` is the XUDLUSS convention: US dollars for one pound.
    A hedged bond return must not be passed through this function.
    """
    if price_usd < 0 or usd_per_gbp <= 0:
        raise ResearchError("FX conversion needs a positive dollars-per-pound rate and a non-negative price")
    return price_usd / usd_per_gbp


def ratio_splice(proxy: Sequence[Point], actual: Sequence[Point], splice: date) -> tuple[Labelled, ...]:
    """Scale ``proxy`` onto the first actual point on or after ``splice``.

    The proxy value used for the scale is the last proxy observation on or
    before that actual date. Later proxy points are discarded.
    """
    actual_from = [(day, level) for day, level in actual if day >= splice]
    if not actual_from:
        raise ResearchError("splice has no actual observation on or after the splice date")
    anchor_day, actual_level = actual_from[0]
    proxy_level: Decimal | None = None
    for day, level in proxy:
        if day <= anchor_day:
            proxy_level = level
    if proxy_level is None or proxy_level <= 0 or actual_level <= 0:
        raise ResearchError("splice needs positive proxy and actual levels")
    scale = actual_level / proxy_level
    merged: dict[date, tuple[Decimal, str]] = {}
    for day, level in proxy:
        if day < anchor_day:
            merged[day] = (level * scale, "proxy")
    for day, level in actual:
        if day >= anchor_day:
            merged[day] = (level, "actual")
    return tuple((day, merged[day][0], merged[day][1]) for day in sorted(merged))


def compound_annual_percent(observations: Sequence[Point]) -> tuple[Point, ...]:
    """Compound an annual percent rate, carrying the last rate across gaps.

    The level is 1 on the first date and then grows by
    ``(1 + rate/100/365.25)`` for each elapsed day. This is an index, not a
    bank deposit and not SONIA itself.
    """
    if not observations:
        return ()
    ordered = sorted(observations, key=lambda item: item[0])
    level = Decimal(1)
    previous_day, previous_rate = ordered[0]
    out = [(previous_day, level)]
    daily = Decimal("365.25")
    for day, rate in ordered[1:]:
        elapsed = (day - previous_day).days
        if elapsed < 0:
            raise ResearchError("rate observations go backwards")
        if elapsed > 0:
            level *= (Decimal(1) + previous_rate / Decimal(100) / daily) ** elapsed
        out.append((day, level))
        previous_day, previous_rate = day, rate
    return tuple(out)


def hedged_return_index(
    local_levels: Sequence[Point],
    domestic_percent: Sequence[Point],
    foreign_percent: Sequence[Point],
) -> tuple[Point, ...]:
    """Turn a foreign-currency total-return level into a GBP-hedged index.

    Each day's hedged move is the local total return minus that day's
    interest differential ``(domestic - foreign) / 100 / 365.25``. The result
    is not multiplied by the spot FX rate. It is an approximation of a hedge,
    not a fund.
    """
    domestic = dict(domestic_percent)
    foreign = dict(foreign_percent)
    ordered = sorted(local_levels, key=lambda item: item[0])
    if len(ordered) < 2:
        raise ResearchError("hedged index needs at least two local levels")
    index = Decimal(1)
    out = [(ordered[0][0], index)]
    previous = ordered[0][1]
    for day, level in ordered[1:]:
        if previous <= 0 or level <= 0:
            raise ResearchError("hedged index levels must stay positive")
        local_return = level / previous - 1
        elapsed = (day - out[-1][0]).days
        if day not in domestic or day not in foreign:
            raise ResearchError(f"missing hedge rate on {day.isoformat()}")
        differential = (domestic[day] - foreign[day]) / Decimal(100) / Decimal("365.25") * Decimal(max(elapsed, 1))
        index *= Decimal(1) + local_return - differential
        if index <= 0:
            raise ResearchError("hedged index became non-positive")
        out.append((day, index))
        previous = level
    if index == ZERO:
        raise ResearchError("hedged index is zero")
    return tuple(out)

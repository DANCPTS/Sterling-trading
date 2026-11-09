"""Time-series trend filter.

At a month-end signal, an instrument is eligible when its close is strictly
above the average of the last ``lookback_months`` month-end closes, including
the signal month. Eligible names are equal-weighted. Otherwise the target is
100% cash. History that does not cover the lookback stays in cash.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sterling.research.money import ZERO
from sterling.research.strategy import MarketView, equal_weights, month_end_levels


@dataclass(frozen=True, slots=True)
class TrendFilter:
    lookback_months: int = 10
    strategy_id: str = "trend_filter"

    def __post_init__(self) -> None:
        if self.lookback_months < 1:
            raise ValueError("lookback_months must be at least 1")

    def target_weights(self, view: MarketView, instrument_ids: Sequence[str]) -> Mapping[str, Decimal]:
        eligible: list[str] = []
        for ticker in instrument_ids:
            levels = month_end_levels(view.closes.get(ticker, ()))
            if len(levels) < self.lookback_months:
                continue
            window = levels[-self.lookback_months :]
            latest_day, price = window[-1]
            if latest_day.year != view.as_of.year or latest_day.month != view.as_of.month:
                continue
            average = sum((level for _day, level in window), ZERO) / Decimal(len(window))
            if price > average:
                eligible.append(ticker)
        return equal_weights(eligible)

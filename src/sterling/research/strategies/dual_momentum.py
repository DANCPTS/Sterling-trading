"""Relative momentum with an absolute-momentum filter against cash.

Rank instruments by trailing lookback return, drop any name that does not beat
``absolute_floor`` (default zero, meaning it must beat cash), and equal-weight
the top ``top_k``. A tie breaks on ticker so the result is deterministic.
Missing history excludes the name rather than borrowing a future price.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sterling.research.strategy import MarketView, equal_weights, month_end_levels


@dataclass(frozen=True, slots=True)
class DualMomentum:
    lookback_months: int = 12
    top_k: int = 1
    absolute_floor: Decimal = Decimal("0")
    strategy_id: str = "dual_momentum"

    def __post_init__(self) -> None:
        if self.lookback_months < 1:
            raise ValueError("lookback_months must be at least 1")
        if self.top_k < 1:
            raise ValueError("top_k must be at least 1")

    def target_weights(self, view: MarketView, instrument_ids: Sequence[str]) -> Mapping[str, Decimal]:
        scored: list[tuple[Decimal, str]] = []
        needed = self.lookback_months + 1
        for ticker in instrument_ids:
            levels = month_end_levels(view.closes.get(ticker, ()))
            if len(levels) < needed:
                continue
            latest_day, end_price = levels[-1]
            if latest_day.year != view.as_of.year or latest_day.month != view.as_of.month:
                continue
            _start_day, start_price = levels[-needed]
            if start_price <= 0:
                continue
            trailing = end_price / start_price - 1
            if trailing > self.absolute_floor:
                scored.append((trailing, ticker))
        scored.sort(key=lambda item: (-item[0], item[1]))
        chosen = [ticker for _ret, ticker in scored[: self.top_k]]
        return equal_weights(chosen)

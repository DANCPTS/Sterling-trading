"""Relative momentum with an absolute-momentum filter against cash.

Rank instruments by trailing lookback return, drop any name that does not beat
the hurdle, and equal-weight the top ``top_k``. A tie breaks on ticker so the
result is deterministic. Missing history excludes the name rather than
borrowing a future price.

The hurdle is ``absolute_floor`` (default zero). If ``defensive`` is set, that
ticker is not ranked. Its own trailing return, when available, replaces the
floor, and any residual weight is allocated to it. That holding is still an
investment, not literal cash.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sterling.research.strategy import MarketView, allocate_with_defensive, month_end_levels


@dataclass(frozen=True, slots=True)
class DualMomentum:
    lookback_months: int = 12
    top_k: int = 2
    absolute_floor: Decimal = Decimal("0")
    defensive: str | None = None
    strategy_id: str = "dual_momentum"

    def __post_init__(self) -> None:
        if self.lookback_months < 1:
            raise ValueError("lookback_months must be at least 1")
        if self.top_k < 1:
            raise ValueError("top_k must be at least 1")

    def _trailing(self, view: MarketView, ticker: str) -> Decimal | None:
        levels = month_end_levels(view.closes.get(ticker, ()))
        needed = self.lookback_months + 1
        if len(levels) < needed:
            return None
        latest_day, end_price = levels[-1]
        if latest_day.year != view.as_of.year or latest_day.month != view.as_of.month:
            return None
        _start_day, start_price = levels[-needed]
        if start_price <= 0:
            return None
        return end_price / start_price - 1

    def target_weights(self, view: MarketView, instrument_ids: Sequence[str]) -> Mapping[str, Decimal]:
        hurdle = self.absolute_floor
        ranked = list(instrument_ids)
        if self.defensive is not None:
            ranked = [ticker for ticker in ranked if ticker != self.defensive]
            defensive_return = self._trailing(view, self.defensive)
            if defensive_return is not None:
                hurdle = defensive_return
        scored: list[tuple[Decimal, str]] = []
        for ticker in ranked:
            trailing = self._trailing(view, ticker)
            if trailing is not None and trailing > hurdle:
                scored.append((trailing, ticker))
        scored.sort(key=lambda item: (-item[0], item[1]))
        chosen = [ticker for _ret, ticker in scored[: self.top_k]]
        return allocate_with_defensive(chosen, self.defensive)

"""Strategy interface, point-in-time views, and the registry.

Reserved names are extension points only. Breakout, mean reversion, individual
stock momentum, swing, and FX research are not implemented. FX and commodity
derivatives stay research-only; this package does not submit an order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from sterling.research.errors import ResearchError
from sterling.research.money import ZERO, quantize_weight_down

RESERVED_EXTENSIONS = (
    "breakout",
    "mean_reversion",
    "individual_stock_momentum",
    "swing",
    "fx_research",
)


@dataclass(frozen=True, slots=True)
class MarketView:
    """Closes the strategy is allowed to see. Every date is on or before ``as_of``."""

    as_of: date
    closes: Mapping[str, tuple[tuple[date, Decimal], ...]]

    def __post_init__(self) -> None:
        for ticker, points in self.closes.items():
            for session, _price in points:
                if session > self.as_of:
                    raise ResearchError(f"{ticker} view includes {session.isoformat()} after {self.as_of.isoformat()}")


class Strategy(Protocol):
    @property
    def strategy_id(self) -> str: ...

    def target_weights(self, view: MarketView, instrument_ids: Sequence[str]) -> Mapping[str, Decimal]:
        """Non-negative instrument weights. The residual is cash, including 100% cash."""


def month_end_levels(points: Sequence[tuple[date, Decimal]]) -> tuple[tuple[date, Decimal], ...]:
    last: dict[tuple[int, int], tuple[date, Decimal]] = {}
    for session, price in points:
        last[(session.year, session.month)] = (session, price)
    return tuple(last[key] for key in sorted(last))


def equal_weights(instrument_ids: Sequence[str]) -> dict[str, Decimal]:
    if not instrument_ids:
        return {}
    weight = quantize_weight_down(Decimal(1) / Decimal(len(instrument_ids)))
    return {ticker: weight for ticker in instrument_ids}


def residual_cash(weights: Mapping[str, Decimal]) -> Decimal:
    invested = sum(weights.values(), ZERO)
    return Decimal(1) - invested


class StrategyRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., Strategy]] = {}

    def register(self, name: str, factory: Callable[..., Strategy]) -> None:
        if name in self._factories:
            raise ResearchError(f"{name} is already registered")
        self._factories[name] = factory

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def create(self, name: str, **params: object) -> Strategy:
        if name in RESERVED_EXTENSIONS and name not in self._factories:
            raise ResearchError(
                f"{name} is a reserved research extension and is not registered. "
                "FX and commodity-derivative research stays research-only. No order path exists."
            )
        factory = self._factories.get(name)
        if factory is None:
            raise ResearchError(f"unknown strategy {name}")
        return factory(**params)


def default_registry() -> StrategyRegistry:
    from sterling.research.strategies.dual_momentum import DualMomentum
    from sterling.research.strategies.trend import TrendFilter

    registry = StrategyRegistry()
    registry.register("trend_filter", TrendFilter)
    registry.register("dual_momentum", DualMomentum)
    return registry

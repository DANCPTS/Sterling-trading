"""Pre-registered unleveraged FX rules.

Trend and carry are specified here, not chosen after seeing the sample.
Weights are long-only and sum to 1 when anything is held. An empty result is
100% GBP cash. Nothing in this module sells a currency short.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal

from sterling.research.errors import ResearchError
from sterling.research.metrics import sample_stdev
from sterling.research.money import ZERO

WEIGHT_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class FxView:
    """Information available on the morning of ``decision``, before that day's fix."""

    decision: date
    month_end: date
    gbp_per_unit: tuple[tuple[str, tuple[tuple[date, Decimal], ...]], ...]
    retail_rate: tuple[tuple[str, Decimal | None], ...]

    def levels(self, code: str) -> tuple[tuple[date, Decimal], ...]:
        for name, series in self.gbp_per_unit:
            if name == code:
                return series
        raise ResearchError(f"view has no GBP value for {code}")

    def rate(self, code: str) -> Decimal | None:
        for name, value in self.retail_rate:
            if name == code:
                return value
        return None

    def currency_codes(self) -> tuple[str, ...]:
        return tuple(code for code, _series in self.gbp_per_unit)


def _finalize(raw: Mapping[str, Decimal]) -> dict[str, Decimal]:
    ordered = tuple(sorted(raw))
    if not ordered:
        return {}
    if any(raw[name] < 0 for name in ordered):
        raise ResearchError("short selling is prohibited")
    total = sum((raw[name] for name in ordered), ZERO)
    if total <= 0:
        return {}
    weights: dict[str, Decimal] = {}
    running = ZERO
    for name in ordered[:-1]:
        weight = (raw[name] / total).quantize(WEIGHT_QUANTUM, rounding=ROUND_DOWN)
        weights[name] = weight
        running += weight
    last = Decimal(1) - running
    if last <= 0:
        raise ResearchError("weights failed to sum to 1")
    weights[ordered[-1]] = last
    if sum(weights.values(), ZERO) != Decimal(1):
        raise ResearchError("weights must sum to 1 or to cash")
    return weights


def equal_weights(names: Sequence[str]) -> dict[str, Decimal]:
    return _finalize({name: Decimal(1) for name in sorted(set(names))})


def inverse_vol_weights(
    view: FxView,
    names: Sequence[str],
    *,
    window: int,
    minimum: int,
) -> dict[str, Decimal]:
    raw: dict[str, Decimal] = {}
    for code in names:
        levels = view.levels(code)
        changes: list[Decimal] = []
        for previous, nxt in zip(levels, levels[1:], strict=False):
            if previous[1] <= 0:
                continue
            changes.append(nxt[1] / previous[1] - 1)
        sample = changes[-window:]
        if len(sample) < minimum:
            continue
        volatility = sample_stdev(sample)
        if volatility <= 0:
            continue
        raw[code] = Decimal(1) / volatility
    return _finalize(raw)


@dataclass(frozen=True, slots=True)
class Trend:
    months: int = 10
    rule: str = "sma"
    weighting: str = "equal"
    vol_window: int = 36
    vol_min: int = 12
    strategy_id: str = "fx_trend"

    def __post_init__(self) -> None:
        if self.months < 1:
            raise ResearchError("trend lookback must be at least 1 month")
        if self.rule not in {"sma", "return_12m"}:
            raise ResearchError("trend rule must be sma or return_12m")
        if self.weighting not in {"equal", "inverse_vol"}:
            raise ResearchError("trend weighting must be equal or inverse_vol")

    def selected(self, view: FxView) -> tuple[str, ...]:
        chosen: list[str] = []
        for code in view.currency_codes():
            levels = view.levels(code)
            if any(day >= view.decision for day, _level in levels):
                raise ResearchError("trend saw a fix that was not yet published")
            if self.rule == "sma":
                if len(levels) < self.months:
                    continue
                window = [level for _day, level in levels[-self.months :]]
                average = sum(window, ZERO) / Decimal(len(window))
                if levels[-1][1] > average:
                    chosen.append(code)
            else:
                if len(levels) < 13 or levels[-13][1] <= 0:
                    continue
                if levels[-1][1] / levels[-13][1] - 1 > 0:
                    chosen.append(code)
        return tuple(chosen)

    def target(self, view: FxView) -> dict[str, Decimal]:
        chosen = self.selected(view)
        if self.weighting == "equal":
            return equal_weights(chosen)
        return inverse_vol_weights(view, chosen, window=self.vol_window, minimum=self.vol_min)


@dataclass(frozen=True, slots=True)
class Carry:
    k: int = 3
    threshold: Decimal = Decimal("0.005")
    strategy_id: str = "fx_carry"

    def __post_init__(self) -> None:
        if self.k < 1:
            raise ResearchError("carry k must be at least 1")

    def selected(self, view: FxView) -> tuple[str, ...]:
        gbp = view.rate("GBP")
        if gbp is None:
            return ()
        scored: list[tuple[Decimal, str]] = []
        for code in view.currency_codes():
            rate = view.rate(code)
            if rate is None:
                continue
            spread = rate - gbp
            if spread > self.threshold:
                scored.append((spread, code))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(code for _spread, code in scored[: self.k])

    def target(self, view: FxView) -> dict[str, Decimal]:
        return equal_weights(self.selected(view))


@dataclass(frozen=True, slots=True)
class Combined:
    trend: Trend
    carry: Carry
    strategy_id: str = "fx_trend_and_carry"

    def target(self, view: FxView) -> dict[str, Decimal]:
        return combined_target(view, self.trend, self.carry)


def combined_target(view: FxView, trend: Trend, carry: Carry) -> dict[str, Decimal]:
    """Hold a currency only when trend and carry both select it."""
    both = set(trend.selected(view)) & set(carry.selected(view))
    return equal_weights(sorted(both))


@dataclass(frozen=True, slots=True)
class LegScore:
    name: str
    excess_cagr: Decimal | None
    sharpe_vs_cash: Decimal | None


def combined_blockers(
    full_sample: tuple[LegScore, LegScore],
    out_of_sample: tuple[LegScore, LegScore],
) -> tuple[str, ...]:
    """Headline combination is allowed only when both legs beat cash twice.

    The bar is full-sample and out-of-sample excess CAGR above zero, and
    Sharpe versus cash above zero, for trend and for carry. It is not refit.
    """
    blockers: list[str] = []
    for window, legs in (("full sample", full_sample), ("out of sample", out_of_sample)):
        for leg in legs:
            if leg.excess_cagr is None or leg.excess_cagr <= 0:
                blockers.append(f"{leg.name} {window} CAGR does not exceed GBP cash")
            if leg.sharpe_vs_cash is None or leg.sharpe_vs_cash <= 0:
                blockers.append(f"{leg.name} {window} Sharpe versus GBP cash is not positive")
    return tuple(blockers)

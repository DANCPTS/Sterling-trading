"""Deterministic research risk gate.

The gate does not call a model. A short is a veto and the previous book is
kept. Oversized long weights are clipped. Cash is the residual, so a clipped
book can sit partly or entirely in cash.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from sterling.research.errors import ResearchError
from sterling.research.money import ZERO, quantize_weight_down
from sterling.research.risk_config import ResearchLimits


@dataclass(frozen=True, slots=True)
class GateResult:
    accepted: bool
    veto: bool
    clipped: bool
    weights: tuple[tuple[str, Decimal], ...]
    cash_weight: Decimal
    reasons: tuple[str, ...]

    def weight_map(self) -> dict[str, Decimal]:
        return {ticker: weight for ticker, weight in self.weights}


def _one_way(left: Mapping[str, Decimal], right: Mapping[str, Decimal]) -> Decimal:
    keys = set(left) | set(right)
    return sum((abs(left.get(key, ZERO) - right.get(key, ZERO)) for key in keys), ZERO) / Decimal(2)


def _pack(weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    packed: dict[str, Decimal] = {}
    for ticker, weight in weights.items():
        quantized = quantize_weight_down(weight)
        if quantized > 0:
            packed[ticker] = quantized
    return packed


def apply_risk_gate(
    proposal: Mapping[str, Decimal],
    current: Mapping[str, Decimal],
    limits: ResearchLimits,
    asset_class_by_ticker: Mapping[str, str],
    nav_gbp: Decimal,
) -> GateResult:
    if nav_gbp <= 0:
        raise ResearchError("risk gate requires a positive NAV")
    current_book = _pack(current)
    reasons: list[str] = []
    if any(weight < 0 for weight in proposal.values()):
        reasons.append("veto: long_only; a negative weight is a short")
        return _veto(current_book, tuple(reasons))
    unknown = [ticker for ticker in proposal if ticker not in asset_class_by_ticker and proposal[ticker] != 0]
    if unknown:
        reasons.append("veto: unknown instrument " + ", ".join(sorted(unknown)))
        return _veto(current_book, tuple(reasons))

    weights: dict[str, Decimal] = {}
    clipped = False
    for ticker, weight in proposal.items():
        if weight == 0:
            continue
        if weight > limits.max_single_instrument_weight:
            weights[ticker] = limits.max_single_instrument_weight
            clipped = True
            reasons.append(
                f"clipped {ticker} from {format(weight, 'f')} to {format(limits.max_single_instrument_weight, 'f')} "
                "(max single instrument)"
            )
        else:
            weights[ticker] = weight

    by_class: dict[str, list[str]] = {}
    for ticker in weights:
        by_class.setdefault(asset_class_by_ticker[ticker], []).append(ticker)
    for asset_class, tickers in sorted(by_class.items()):
        cap = limits.asset_cap(asset_class)
        total = sum((weights[ticker] for ticker in tickers), ZERO)
        if total > cap:
            scale = cap / total
            for ticker in tickers:
                weights[ticker] = weights[ticker] * scale
            clipped = True
            reasons.append(f"clipped asset class {asset_class} from {format(total, 'f')} to {format(cap, 'f')}")

    gross = sum(weights.values(), ZERO)
    if gross > limits.max_gross:
        scale = limits.max_gross / gross
        for ticker in list(weights):
            weights[ticker] = weights[ticker] * scale
        clipped = True
        reasons.append(f"clipped gross from {format(gross, 'f')} to {format(limits.max_gross, 'f')}")

    weights = _pack(weights)
    for ticker in sorted(set(weights) | set(current_book)):
        new = weights.get(ticker, ZERO)
        old = current_book.get(ticker, ZERO)
        if new != old and abs(new - old) * nav_gbp < limits.min_trade_notional_gbp:
            if old == 0:
                weights.pop(ticker, None)
            else:
                weights[ticker] = old
            clipped = True
            reasons.append(f"skipped {ticker}; change is below the min trade of {format(limits.min_trade_notional_gbp, 'f')}")

    turnover = _one_way(weights, current_book)
    if turnover > limits.max_one_way_turnover and turnover > 0:
        scale = limits.max_one_way_turnover / turnover
        blended: dict[str, Decimal] = {}
        for ticker in set(weights) | set(current_book):
            old = current_book.get(ticker, ZERO)
            new = weights.get(ticker, ZERO)
            blended[ticker] = old + (new - old) * scale
        weights = _pack(blended)
        clipped = True
        reasons.append(f"scaled turnover from {format(turnover, 'f')} to {format(limits.max_one_way_turnover, 'f')}")

    gross = sum(weights.values(), ZERO)
    cash = Decimal(1) - gross
    if gross - limits.max_gross > Decimal("0.00000001") or cash < limits.cash_min or cash > limits.cash_max:
        reasons.append("veto: book still outside gross or cash limits after clipping")
        return _veto(current_book, tuple(reasons))
    if not reasons:
        reasons.append("accepted")
    return GateResult(
        accepted=True,
        veto=False,
        clipped=clipped,
        weights=tuple(sorted(weights.items())),
        cash_weight=cash,
        reasons=tuple(reasons),
    )


def _veto(current: Mapping[str, Decimal], reasons: tuple[str, ...]) -> GateResult:
    gross = sum(current.values(), ZERO)
    return GateResult(
        accepted=False,
        veto=True,
        clipped=False,
        weights=tuple(sorted(current.items())),
        cash_weight=Decimal(1) - gross,
        reasons=reasons,
    )

"""Deterministic research risk gate.

The gate does not call a model. A short is a veto and the previous book is
kept. Oversized long weights are clipped. Cash is the residual, so a clipped
book can sit partly or entirely in cash.

``fail_closed_allocation`` is the paper-v2 pilot check. It does not clip.
A proposal outside the research limits is rejected and the caller keeps the
current book.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
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
    no_trade_band: Decimal = ZERO,
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

    exempt = set(limits.single_instrument_cap_exempt_asset_classes)
    weights: dict[str, Decimal] = {}
    clipped = False
    for ticker, weight in proposal.items():
        if weight == 0:
            continue
        asset_class = asset_class_by_ticker[ticker]
        if asset_class in exempt:
            ceiling = min(limits.max_gross, limits.asset_cap(asset_class))
            rule = f"{asset_class} class cap"
        else:
            ceiling = limits.max_single_instrument_weight
            rule = "max single instrument"
        if weight > ceiling:
            weights[ticker] = ceiling
            clipped = True
            reasons.append(f"clipped {ticker} from {format(weight, 'f')} to {format(ceiling, 'f')} ({rule})")
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
    if no_trade_band > 0:
        for ticker in sorted(set(weights) | set(current_book)):
            new = weights.get(ticker, ZERO)
            old = current_book.get(ticker, ZERO)
            if new != old and abs(new - old) < no_trade_band:
                if old == 0:
                    weights.pop(ticker, None)
                else:
                    weights[ticker] = old
                clipped = True
                reasons.append(
                    f"no_trade_band kept {ticker} at {format(old, 'f')}; "
                    f"move {format(abs(new - old), 'f')} is inside {format(no_trade_band, 'f')}"
                )
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
    risk_off = _risk_off_rebalance(weights, current_book, asset_class_by_ticker)
    if limits.risk_off_turnover_exception and risk_off and turnover > limits.max_one_way_turnover and turnover > 0:
        reasons.append(
            "risk_off_turnover_exception: every non-cash-proxy weight is unchanged or lower, "
            "so the one-way turnover cap does not scale this rebalance"
        )
    elif turnover > limits.max_one_way_turnover and turnover > 0:
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


def _risk_off_rebalance(
    proposed: Mapping[str, Decimal],
    current: Mapping[str, Decimal],
    asset_class_by_ticker: Mapping[str, str],
) -> bool:
    """True when no risky sleeve increases.

    Cash-proxy weights and literal cash may rise. Equity, bond, commodity, and
    any other non-cash-proxy weight must be unchanged or lower. A buy of a
    risky sleeve makes the whole rebalance subject to the turnover cap.
    """
    tolerance = Decimal("0.00000001")
    for ticker in set(proposed) | set(current):
        if asset_class_by_ticker.get(ticker) == "cash_proxy":
            continue
        if proposed.get(ticker, ZERO) > current.get(ticker, ZERO) + tolerance:
            return False
    return True


@dataclass(frozen=True, slots=True)
class FailClosedDecision:
    """Accepted only when the proposal already fits. Weights are never resized."""

    accepted: bool
    reasons: tuple[str, ...]


def fail_closed_allocation(
    proposal: Mapping[str, Decimal],
    current: Mapping[str, Decimal],
    limits: ResearchLimits,
    asset_class_by_ticker: Mapping[str, str],
    nav_gbp: Decimal | None,
    *,
    tradable: set[str],
    stale: set[str],
    priced: set[str],
    eligible: set[str],
    decision_at: datetime,
    fill_session: date | None,
) -> FailClosedDecision:
    """Reject a paper-v2 allocation that breaches the research limits.

    This is not an operational trading policy. ``owner_approved`` must stay
    false. The returned weights are not a clipped substitute for the proposal.
    """
    if limits.owner_approved:
        return FailClosedDecision(False, ("rejected: research policy is not an operational trading policy",))
    if nav_gbp is None or nav_gbp <= 0:
        return FailClosedDecision(False, ("rejected: missing_valuation",))
    reasons: list[str] = []
    if any(weight < 0 for weight in proposal.values()) or any(weight < 0 for weight in current.values()):
        reasons.append("rejected: long_only")
    names = [ticker for ticker in proposal if ticker != "CASH"]
    unknown = [ticker for ticker in names if ticker not in eligible or ticker not in asset_class_by_ticker]
    if unknown:
        reasons.append("rejected: instrument_eligibility " + ", ".join(sorted(set(unknown))))
    if reasons:
        return FailClosedDecision(False, tuple(reasons))

    from sterling.research.invest.registration import NO_TRADE_BAND
    from sterling.research.invest.sessions import first_session_after, session_open

    tolerance = Decimal("0.00000001")
    instruments = {ticker: proposal.get(ticker, ZERO) for ticker in eligible}
    current_book = {ticker: current.get(ticker, ZERO) for ticker in eligible}
    for ticker, weight in instruments.items():
        if weight == 0:
            continue
        asset_class = asset_class_by_ticker[ticker]
        exempt = asset_class in limits.single_instrument_cap_exempt_asset_classes
        ceiling = limits.asset_cap(asset_class) if exempt else limits.max_single_instrument_weight
        if weight > ceiling + tolerance:
            reasons.append(f"rejected: concentration {ticker} {format(weight, 'f')} above {format(ceiling, 'f')}")
    by_class: dict[str, Decimal] = {}
    for ticker, weight in instruments.items():
        if weight == 0:
            continue
        asset_class = asset_class_by_ticker[ticker]
        by_class[asset_class] = by_class.get(asset_class, ZERO) + weight
    for asset_class, total in sorted(by_class.items()):
        cap = limits.asset_cap(asset_class)
        if total > cap + tolerance:
            reasons.append(f"rejected: concentration {asset_class} {format(total, 'f')} above {format(cap, 'f')}")
    gross = sum(instruments.values(), ZERO)
    cash = proposal.get("CASH", Decimal(1) - gross)
    if gross > limits.max_gross + tolerance:
        reasons.append(f"rejected: gross {format(gross, 'f')} above {format(limits.max_gross, 'f')}")
    if cash < limits.cash_min - tolerance or cash > limits.cash_max + tolerance:
        reasons.append(
            f"rejected: cash {format(cash, 'f')} outside {format(limits.cash_min, 'f')} to {format(limits.cash_max, 'f')}"
        )
    if abs(gross + cash - 1) > Decimal("0.005") + tolerance:
        reasons.append("rejected: cash weight does not match the residual")
    for ticker in eligible:
        if current_book.get(ticker, ZERO) > tolerance and ticker not in priced:
            reasons.append(f"rejected: missing_valuation {ticker}")
            break
    for ticker in sorted(eligible):
        gap = abs(instruments.get(ticker, ZERO) - current_book.get(ticker, ZERO))
        if gap <= NO_TRADE_BAND:
            continue
        if ticker in stale or ticker not in tradable:
            reasons.append(f"rejected: stale_or_untradable {ticker}")
        elif ticker not in priced:
            reasons.append(f"rejected: missing_valuation {ticker}")
        elif gap * nav_gbp < limits.min_trade_notional_gbp:
            reasons.append(f"rejected: below_min_trade {ticker}")
    turnover = _one_way(instruments, current_book)
    risk_off = _risk_off_rebalance(instruments, current_book, asset_class_by_ticker)
    if turnover > limits.max_one_way_turnover + tolerance and not (limits.risk_off_turnover_exception and risk_off):
        reasons.append(f"rejected: turnover {format(turnover, 'f')} above {format(limits.max_one_way_turnover, 'f')}")
    try:
        expected = first_session_after(decision_at)
    except ResearchError:
        reasons.append("rejected: next_open_timing")
        expected = None
    if expected is not None:
        opened = session_open(expected)
        if opened is None or opened <= decision_at:
            reasons.append("rejected: next_open_timing")
        elif fill_session is not None and fill_session != expected:
            reasons.append("rejected: next_open_timing")
    if reasons:
        return FailClosedDecision(False, tuple(dict.fromkeys(reasons)))
    return FailClosedDecision(True, ("accepted",))


def paper_asset_classes() -> dict[str, str]:
    """Asset classes for the six paper-v2 funds. Optional comparison names are excluded."""
    from sterling.research.instruments import load_candidate_universe
    from sterling.research.invest.schema import FUNDS

    found = {item.ticker: item.asset_class for item in load_candidate_universe().instruments}
    missing = [ticker for ticker in FUNDS if ticker not in found]
    if missing:
        raise ResearchError("candidate universe is missing " + ", ".join(missing))
    return {ticker: found[ticker] for ticker in FUNDS}


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

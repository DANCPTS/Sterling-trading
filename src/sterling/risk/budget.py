"""Pure Decimal risk budgets for Stage 2 Gate 1.

Available budgets round down to the penny. Risk, notional, and cash
consumption round up. The Stage 1 cap helpers stay on half-even rounding
and are not used here, so a Gate 1 budget is never looser than that display
figure and never above the owner-approved absolute ceilings.

Booked fees are not parameters of the incremental-risk functions. Day loss
already contains them through equity. Passing the mark, rather than the
entry, as the reference price keeps unrealised loss from being counted again.

Nothing here submits an order, reserves a quantity, or guarantees a realised
loss. A gap through the stop can cost more than the planned figure.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from sterling.errors import MoneyError
from sterling.ledger.money import MONEY_QUANTUM, require_money, require_scale, signed_money
from sterling.risk.errors import RiskInputError
from sterling.risk.policy import Stage2Policy
from sterling.risk.reasons import Reason

ZERO = Decimal("0.00")
PLANNED_CAP_GUARANTEES_REALISED_LOSS = False


def round_risk_up(value: object, *, detail: str = "risk") -> Decimal:
    """Round a non-negative amount up to the next penny."""
    amount = _as_decimal(value, detail)
    if amount < 0:
        raise RiskInputError(Reason.NEGATIVE_REJECTED, detail)
    return _quantize(amount, ROUND_CEILING)


def round_budget_down(value: object, *, detail: str = "budget") -> Decimal:
    """Round a non-negative amount down to the penny."""
    amount = _as_decimal(value, detail)
    if amount < 0:
        raise RiskInputError(Reason.NEGATIVE_REJECTED, detail)
    return _quantize(amount, ROUND_FLOOR)


def per_trade_risk_cap(equity: object, policy: Stage2Policy) -> Decimal:
    """min(0.5% of fresh equity, £2.50), rounded down."""
    current = _penny_non_negative(equity, "equity")
    raw = current * policy.per_trade_equity_fraction
    return round_budget_down(min(raw, policy.per_trade_absolute_ceiling_gbp), detail="per_trade_cap")


def daily_loss_threshold_cap(day_opening_equity: object, policy: Stage2Policy) -> Decimal:
    """min(1% of fresh day-opening equity, £5), rounded down."""
    opening = _penny_non_negative(day_opening_equity, "day_opening_equity")
    raw = opening * policy.daily_loss_equity_fraction
    return round_budget_down(min(raw, policy.daily_loss_absolute_ceiling_gbp), detail="daily_threshold")


def aggregate_risk_cap(equity: object, policy: Stage2Policy) -> Decimal:
    """1% of fresh equity, including room for open and pending risk, rounded down."""
    current = _penny_non_negative(equity, "equity")
    return round_budget_down(current * policy.aggregate_equity_fraction, detail="aggregate_cap")


def concentration_cap(equity: object, policy: Stage2Policy) -> Decimal:
    """25% of fresh equity. There is no second absolute pound ceiling."""
    current = _penny_non_negative(equity, "equity")
    return round_budget_down(current * policy.concentration_equity_fraction, detail="concentration_cap")


def day_loss_gbp(
    day_opening_equity: object,
    marked_equity: object,
    net_external_deposits: object,
) -> Decimal:
    """max(0, opening - (marked equity - net external deposits since the baseline)).

    Deposits and withdrawals move equity and the external-flow term together,
    so they do not reset the budget and a withdrawal is not itself a trading loss.
    """
    opening = _penny_non_negative(day_opening_equity, "day_opening_equity")
    marked = _penny_non_negative(marked_equity, "marked_equity")
    net = _penny_signed(net_external_deposits, "net_external_deposits")
    adjusted = marked - net
    raw = opening - adjusted
    if raw <= 0:
        return ZERO
    return raw


def remaining_day_risk_gbp(
    threshold: object,
    day_loss: object,
    existing_incremental_risk: object,
) -> Decimal:
    """max(0, threshold - day loss - open/pending incremental risk), rounded down.

    ``existing_incremental_risk`` must already exclude booked fees and the
    mark-to-market loss inside ``day_loss``.
    """
    cap = _penny_non_negative(threshold, "daily_threshold")
    loss = _penny_non_negative(day_loss, "day_loss")
    existing = _penny_non_negative(existing_incremental_risk, "existing_incremental_risk")
    raw = cap - loss - existing
    if raw <= 0:
        return ZERO
    return round_budget_down(raw, detail="remaining_day_risk")


def planned_entry_risk_gbp(
    *,
    entry_price: object,
    stop_price: object,
    quantity: object,
    fx_rate_to_gbp: object,
    expected_entry_cost_gbp: object,
    expected_exit_cost_gbp: object,
    adverse_allowance_gbp: object,
) -> Decimal:
    """Entry-to-stop loss plus unbooked costs and the adverse allowance, rounded up.

    The stop must be below the entry. Costs are added once, then the sum is
    rounded up. This is not a cap on a gap through the stop.
    """
    entry = _positive_scale(entry_price, "entry_price")
    stop = _positive_scale(stop_price, "stop_price")
    if stop >= entry:
        raise RiskInputError(Reason.STOP_NOT_BELOW_ENTRY, "stop_price")
    qty = _positive_scale(quantity, "quantity")
    fx = _positive_scale(fx_rate_to_gbp, "fx_rate_to_gbp")
    extra = _cost(expected_entry_cost_gbp, "expected_entry_cost_gbp")
    extra += _cost(expected_exit_cost_gbp, "expected_exit_cost_gbp")
    extra += _cost(adverse_allowance_gbp, "adverse_allowance_gbp")
    raw = (entry - stop) * qty * fx + extra
    return round_risk_up(raw, detail="planned_entry_risk")


def incremental_stop_risk_gbp(
    *,
    reference_price: object,
    stop_price: object,
    quantity: object,
    fx_rate_to_gbp: object,
    unbooked_entry_cost_gbp: object,
    expected_exit_cost_gbp: object,
    adverse_allowance_gbp: object,
) -> Decimal:
    """Remaining loss from the reference price down to the stop, plus unbooked costs.

    Use the mark for a filled position and the planned entry for a pending
    opening. A mark that is already through the stop contributes no further
    price risk here; that loss belongs in day loss. Booked fees are not an
    argument and are not added.
    """
    reference = _positive_scale(reference_price, "reference_price")
    stop = _positive_scale(stop_price, "stop_price")
    qty = _positive_scale(quantity, "quantity")
    fx = _positive_scale(fx_rate_to_gbp, "fx_rate_to_gbp")
    extra = _cost(unbooked_entry_cost_gbp, "unbooked_entry_cost_gbp")
    extra += _cost(expected_exit_cost_gbp, "expected_exit_cost_gbp")
    extra += _cost(adverse_allowance_gbp, "adverse_allowance_gbp")
    gap = reference - stop
    if gap < 0:
        gap = Decimal("0")
    return round_risk_up(gap * qty * fx + extra, detail="incremental_stop_risk")


def conservative_notional_gbp(*, quantity: object, price: object, fx_rate_to_gbp: object) -> Decimal:
    """price * quantity * FX, rounded up. This is exposure, not stop risk."""
    qty = _positive_scale(quantity, "quantity")
    px = _positive_scale(price, "conservative_price")
    fx = _positive_scale(fx_rate_to_gbp, "conservative_fx_rate_to_gbp")
    return round_risk_up(qty * px * fx, detail="notional")


def cash_required_gbp(
    *,
    quantity: object,
    entry_price: object,
    fx_rate_to_gbp: object,
    expected_entry_cost_gbp: object,
) -> Decimal:
    """Funded cash to pay the entry, rounded up. Exit costs are not charged again here."""
    qty = _positive_scale(quantity, "quantity")
    px = _positive_scale(entry_price, "entry_price")
    fx = _positive_scale(fx_rate_to_gbp, "fx_rate_to_gbp")
    cost = _cost(expected_entry_cost_gbp, "expected_entry_cost_gbp")
    return round_risk_up(qty * px * fx + cost, detail="cash_required")


def realised_long_loss_gbp(
    *,
    entry_price: object,
    exit_price: object,
    quantity: object,
    fx_rate_to_gbp: object,
    entry_cost_gbp: object,
    exit_cost_gbp: object,
    slippage_gbp: object,
) -> Decimal:
    """Arithmetic loss for a supplied exit price. This does not execute a fill.

    ``PLANNED_CAP_GUARANTEES_REALISED_LOSS`` is false. A result above the plan
    is the gap or slippage the plan did not bound.
    """
    entry = _positive_scale(entry_price, "entry_price")
    exit_price_d = _positive_scale(exit_price, "exit_price")
    qty = _positive_scale(quantity, "quantity")
    fx = _positive_scale(fx_rate_to_gbp, "fx_rate_to_gbp")
    extra = _cost(entry_cost_gbp, "entry_cost_gbp")
    extra += _cost(exit_cost_gbp, "exit_cost_gbp")
    extra += _cost(slippage_gbp, "slippage_gbp")
    gap = entry - exit_price_d
    if gap < 0:
        gap = Decimal("0")
    return round_risk_up(gap * qty * fx + extra, detail="realised_loss")


def loss_exceeds_plan(planned: object, realised: object) -> bool:
    """Whether this one arithmetic result is above the planned figure.

    False does not mean the plan guarantees a future loss.
    """
    planned_d = _penny_non_negative(planned, "planned")
    realised_d = _penny_non_negative(realised, "realised")
    return realised_d > planned_d


def consecutive_net_loss_episodes(nets: Sequence[object]) -> int:
    """Trailing fully-costed net-loss episodes.

    A positive net clears the point-in-time run. A zero net neither adds to
    it nor clears it, matching the Stage 1 closed-trade streak. This count is
    not a stored latch.
    """
    streak = 0
    for index, net in enumerate(nets):
        amount = _penny_signed(net, f"episode_net[{index}]")
        if amount < 0:
            streak += 1
        elif amount > 0:
            streak = 0
    return streak


def restate_episode_net(
    *,
    net_gbp: object,
    late_entry_fee_gbp: object,
    fee_fill_id: str,
    origin_fill_id: str,
) -> Decimal:
    """Subtract a late entry fee from the original fill's episode net.

    The fee follows that fill identity. It is not moved onto a later re-entry.
    """
    if fee_fill_id.strip() == "" or origin_fill_id.strip() == "" or fee_fill_id != origin_fill_id:
        raise RiskInputError(Reason.LATE_FEE_FILL_MISMATCH, "fee_fill_id")
    net = _penny_signed(net_gbp, "net_gbp")
    fee = _penny_non_negative(late_entry_fee_gbp, "late_entry_fee_gbp")
    if fee <= 0:
        raise RiskInputError(Reason.NOT_POSITIVE, "late_entry_fee_gbp")
    restated = net - fee
    if restated == 0:
        return ZERO
    return restated


def _quantize(amount: Decimal, rounding: str) -> Decimal:
    quantized = amount.quantize(MONEY_QUANTUM, rounding=rounding)
    if quantized == 0:
        return ZERO
    return quantized


def _as_decimal(value: object, detail: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise RiskInputError(Reason.FLOAT_REJECTED, detail)
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, str):
        if "e" in value.lower():
            raise RiskInputError(Reason.INVALID_DECIMAL, detail)
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise RiskInputError(Reason.INVALID_DECIMAL, detail) from exc
    else:
        raise RiskInputError(Reason.INVALID_DECIMAL, detail)
    if not amount.is_finite():
        raise RiskInputError(Reason.NONFINITE, detail)
    return amount


def _positive_scale(value: object, detail: str) -> Decimal:
    amount = _scaled(value, detail, places=8)
    if amount <= 0:
        raise RiskInputError(Reason.NOT_POSITIVE, detail)
    return amount


def _cost(value: object, detail: str) -> Decimal:
    amount = _scaled(value, detail, places=8)
    if amount < 0:
        raise RiskInputError(Reason.NEGATIVE_REJECTED, detail)
    return amount


def _scaled(value: object, detail: str, *, places: int) -> Decimal:
    amount = _as_decimal(value, detail)
    try:
        return require_scale(amount, label=detail, places=places)
    except MoneyError as exc:
        raise _from_money(exc, detail) from exc


def _penny_non_negative(value: object, detail: str) -> Decimal:
    amount = _as_decimal(value, detail)
    try:
        parsed = require_money(amount, label=detail)
    except MoneyError as exc:
        raise _from_money(exc, detail) from exc
    if parsed < 0:
        raise RiskInputError(Reason.NEGATIVE_REJECTED, detail)
    return parsed


def _penny_signed(value: object, detail: str) -> Decimal:
    amount = _as_decimal(value, detail)
    try:
        return signed_money(amount, label=detail)
    except MoneyError as exc:
        raise _from_money(exc, detail) from exc


def _from_money(exc: MoneyError, detail: str) -> RiskInputError:
    text = str(exc)
    if "finite" in text:
        return RiskInputError(Reason.NONFINITE, detail)
    if "decimal places" in text:
        return RiskInputError(Reason.SCALE_EXCEEDED, detail)
    if "float" in text or "bool" in text:
        return RiskInputError(Reason.FLOAT_REJECTED, detail)
    return RiskInputError(Reason.INVALID_DECIMAL, detail)

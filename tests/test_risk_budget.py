"""Hand-checked Gate 1 budgets. These are ceilings, not performance."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sterling.risk.budget import (
    PLANNED_CAP_GUARANTEES_REALISED_LOSS,
    aggregate_risk_cap,
    concentration_cap,
    consecutive_net_loss_episodes,
    daily_loss_threshold_cap,
    day_loss_gbp,
    incremental_stop_risk_gbp,
    loss_exceeds_plan,
    per_trade_risk_cap,
    planned_entry_risk_gbp,
    realised_long_loss_gbp,
    remaining_day_risk_gbp,
    restate_episode_net,
    round_budget_down,
    round_risk_up,
)
from sterling.risk.errors import RiskInputError
from sterling.risk.policy import Stage2Policy, load_default_policy
from sterling.risk.reasons import Reason
from sterling.rules.caps import daily_loss_threshold, per_trade_planned_risk
from sterling.rules.model import DeskRules

# equity, per-trade down, daily down, aggregate down, concentration down
_CASES = [
    ("0.00", "0.00", "0.00", "0.00", "0.00"),
    ("100.00", "0.50", "1.00", "1.00", "25.00"),
    ("200.00", "1.00", "2.00", "2.00", "50.00"),
    ("250.00", "1.25", "2.50", "2.50", "62.50"),
    ("400.00", "2.00", "4.00", "4.00", "100.00"),
    ("499.00", "2.49", "4.99", "4.99", "124.75"),
    ("500.00", "2.50", "5.00", "5.00", "125.00"),
    ("600.00", "2.50", "5.00", "6.00", "150.00"),
    ("333.33", "1.66", "3.33", "3.33", "83.33"),
    ("100000.00", "2.50", "5.00", "1000.00", "25000.00"),
]


@pytest.fixture(scope="module")
def policy() -> Stage2Policy:
    return load_default_policy()


@pytest.mark.parametrize(("equity", "per_trade", "daily", "aggregate", "concentration"), _CASES)
def test_anchor_and_scaled_caps(
    equity: str,
    per_trade: str,
    daily: str,
    aggregate: str,
    concentration: str,
    policy: Stage2Policy,
    rules: DeskRules,
) -> None:
    assert per_trade_risk_cap(equity, policy) == Decimal(per_trade)
    assert daily_loss_threshold_cap(equity, policy) == Decimal(daily)
    assert aggregate_risk_cap(equity, policy) == Decimal(aggregate)
    assert concentration_cap(equity, policy) == Decimal(concentration)
    assert per_trade_risk_cap(equity, policy) <= Decimal("2.50")
    assert daily_loss_threshold_cap(equity, policy) <= Decimal("5.00")
    assert per_trade_risk_cap(equity, policy) <= per_trade_planned_risk(equity, rules)
    assert daily_loss_threshold_cap(equity, policy) <= daily_loss_threshold(equity, rules)


def test_primary_account_hits_absolute_ceilings_and_concentration(policy: Stage2Policy) -> None:
    assert per_trade_risk_cap("500.00", policy) == Decimal("2.50")
    assert daily_loss_threshold_cap("500.00", policy) == Decimal("5.00")
    assert aggregate_risk_cap("500.00", policy) == Decimal("5.00")
    assert concentration_cap("500.00", policy) == Decimal("125.00")
    assert policy.max_simultaneous_position_episodes == 4


def test_stage1_half_even_display_is_unchanged_where_gate1_is_tighter(rules: DeskRules, policy: Stage2Policy) -> None:
    assert per_trade_planned_risk("499.00", rules) == Decimal("2.50")
    assert per_trade_risk_cap("499.00", policy) == Decimal("2.49")
    assert per_trade_planned_risk("333.33", rules) == Decimal("1.67")
    assert per_trade_risk_cap("333.33", policy) == Decimal("1.66")


def test_risk_rounds_up_and_budgets_round_down() -> None:
    assert round_risk_up("0.001") == Decimal("0.01")
    assert round_risk_up("2.501") == Decimal("2.51")
    assert round_risk_up("2.500") == Decimal("2.50")
    assert round_budget_down("0.009") == Decimal("0.00")
    assert round_budget_down("2.499") == Decimal("2.49")
    assert round_budget_down("2.50") == Decimal("2.50")
    assert round_risk_up(round_risk_up("1.239")) == Decimal("1.24")
    assert round_budget_down(round_budget_down("1.239")) == Decimal("1.23")


def test_fractional_quantity_and_costs_round_the_risk_sum_up() -> None:
    risk = planned_entry_risk_gbp(
        entry_price="10",
        stop_price="9",
        quantity="0.33333333",
        fx_rate_to_gbp="1",
        expected_entry_cost_gbp="0",
        expected_exit_cost_gbp="0",
        adverse_allowance_gbp="0",
    )
    assert risk == Decimal("0.34")
    with_cost = planned_entry_risk_gbp(
        entry_price="10",
        stop_price="7.501",
        quantity="1",
        fx_rate_to_gbp="1",
        expected_entry_cost_gbp="0.000",
        expected_exit_cost_gbp="0",
        adverse_allowance_gbp="0",
    )
    assert with_cost == Decimal("2.50")
    over = planned_entry_risk_gbp(
        entry_price="10",
        stop_price="7.499",
        quantity="1",
        fx_rate_to_gbp="1",
        expected_entry_cost_gbp="0",
        expected_exit_cost_gbp="0",
        adverse_allowance_gbp="0",
    )
    assert over == Decimal("2.51")


def test_day_loss_ignores_deposits_and_withdrawals() -> None:
    assert day_loss_gbp("500.00", "494.00", "0.00") == Decimal("6.00")
    assert day_loss_gbp("500.00", "600.00", "100.00") == Decimal("0.00")
    assert day_loss_gbp("500.00", "490.00", "-10.00") == Decimal("0.00")
    assert day_loss_gbp("500.00", "510.00", "0.00") == Decimal("0.00")
    assert remaining_day_risk_gbp("5.00", "2.40", "1.15") == Decimal("1.45")
    assert remaining_day_risk_gbp("5.00", "5.00", "0.00") == Decimal("0.00")
    assert remaining_day_risk_gbp("5.00", "6.00", "0.00") == Decimal("0.00")


def test_open_risk_uses_the_mark_and_excludes_booked_fees() -> None:
    from_mark = incremental_stop_risk_gbp(
        reference_price="10",
        stop_price="9",
        quantity="1",
        fx_rate_to_gbp="1",
        unbooked_entry_cost_gbp="0",
        expected_exit_cost_gbp="0.10",
        adverse_allowance_gbp="0.05",
    )
    assert from_mark == Decimal("1.15")
    through_stop = incremental_stop_risk_gbp(
        reference_price="7",
        stop_price="9",
        quantity="1",
        fx_rate_to_gbp="1",
        unbooked_entry_cost_gbp="0",
        expected_exit_cost_gbp="0.10",
        adverse_allowance_gbp="0.05",
    )
    assert through_stop == Decimal("0.15")


def test_gap_and_slippage_can_exceed_the_plan() -> None:
    planned = planned_entry_risk_gbp(
        entry_price="10",
        stop_price="9",
        quantity="1",
        fx_rate_to_gbp="1",
        expected_entry_cost_gbp="0.10",
        expected_exit_cost_gbp="0.10",
        adverse_allowance_gbp="0.20",
    )
    assert planned == Decimal("1.40")
    gapped = realised_long_loss_gbp(
        entry_price="10",
        exit_price="7",
        quantity="1",
        fx_rate_to_gbp="1",
        entry_cost_gbp="0.10",
        exit_cost_gbp="0.10",
        slippage_gbp="0.50",
    )
    assert gapped == Decimal("3.70")
    assert loss_exceeds_plan(planned, gapped) is True
    assert PLANNED_CAP_GUARANTEES_REALISED_LOSS is False
    slipped_stop = realised_long_loss_gbp(
        entry_price="10",
        exit_price="8.80",
        quantity="1",
        fx_rate_to_gbp="1",
        entry_cost_gbp="0.10",
        exit_cost_gbp="0.10",
        slippage_gbp="0",
    )
    assert slipped_stop == Decimal("1.40")
    worse = realised_long_loss_gbp(
        entry_price="10",
        exit_price="8.79",
        quantity="1",
        fx_rate_to_gbp="1",
        entry_cost_gbp="0.10",
        exit_cost_gbp="0.10",
        slippage_gbp="0",
    )
    assert loss_exceeds_plan(planned, worse) is True


def test_fx_changes_the_pound_risk() -> None:
    risk = planned_entry_risk_gbp(
        entry_price="10",
        stop_price="9",
        quantity="1",
        fx_rate_to_gbp="1.5",
        expected_entry_cost_gbp="0.10",
        expected_exit_cost_gbp="0.10",
        adverse_allowance_gbp="0.05",
    )
    assert risk == Decimal("1.75")


def test_streak_and_late_fee_follow_the_original_fill() -> None:
    assert consecutive_net_loss_episodes([Decimal("-1.00"), Decimal("-0.20"), Decimal("-0.01")]) == 3
    assert consecutive_net_loss_episodes([Decimal("-1.00"), Decimal("0.00"), Decimal("-0.20")]) == 2
    assert consecutive_net_loss_episodes([Decimal("-1.00"), Decimal("-1.00"), Decimal("-1.00"), Decimal("0.10")]) == 0
    restated = restate_episode_net(
        net_gbp="0.10",
        late_entry_fee_gbp="0.30",
        fee_fill_id="FILL-1",
        origin_fill_id="FILL-1",
    )
    assert restated == Decimal("-0.20")
    with pytest.raises(RiskInputError) as caught:
        restate_episode_net(
            net_gbp="0.10",
            late_entry_fee_gbp="0.30",
            fee_fill_id="FILL-2",
            origin_fill_id="FILL-1",
        )
    assert caught.value.reason == Reason.LATE_FEE_FILL_MISMATCH


def test_a_gain_does_not_become_a_loss_and_bad_decimals_fail() -> None:
    gained = realised_long_loss_gbp(
        entry_price="10",
        exit_price="11",
        quantity="1",
        fx_rate_to_gbp="1",
        entry_cost_gbp="0.10",
        exit_cost_gbp="0",
        slippage_gbp="0",
    )
    assert gained == Decimal("0.10")
    with pytest.raises(RiskInputError) as exponential:
        round_risk_up("1e2")
    assert exponential.value.reason == Reason.INVALID_DECIMAL
    with pytest.raises(RiskInputError) as text:
        round_budget_down("nope")
    assert text.value.reason == Reason.INVALID_DECIMAL
    with pytest.raises(RiskInputError) as negative:
        round_risk_up("-0.01")
    assert negative.value.reason == Reason.NEGATIVE_REJECTED
    with pytest.raises(RiskInputError) as scale:
        planned_entry_risk_gbp(
            entry_price="1.123456789",
            stop_price="1",
            quantity="1",
            fx_rate_to_gbp="1",
            expected_entry_cost_gbp="0",
            expected_exit_cost_gbp="0",
            adverse_allowance_gbp="0",
        )
    assert scale.value.reason == Reason.SCALE_EXCEEDED
    with pytest.raises(RiskInputError) as fee:
        restate_episode_net(net_gbp="1.00", late_entry_fee_gbp="0.00", fee_fill_id="FILL-1", origin_fill_id="FILL-1")
    assert fee.value.reason == Reason.NOT_POSITIVE
    exact = restate_episode_net(net_gbp="0.30", late_entry_fee_gbp="0.30", fee_fill_id="FILL-1", origin_fill_id="FILL-1")
    assert exact == Decimal("0.00")


def test_float_nonfinite_and_negative_budget_inputs_are_rejected(policy: Stage2Policy) -> None:
    with pytest.raises(RiskInputError) as floated:
        per_trade_risk_cap(500.0, policy)
    assert floated.value.reason == Reason.FLOAT_REJECTED
    with pytest.raises(RiskInputError) as infinite:
        round_risk_up(Decimal("NaN"))
    assert infinite.value.reason == Reason.NONFINITE
    with pytest.raises(RiskInputError) as negative:
        daily_loss_threshold_cap("-1.00", policy)
    assert negative.value.reason == Reason.NEGATIVE_REJECTED
    with pytest.raises(RiskInputError) as stop:
        planned_entry_risk_gbp(
            entry_price="10",
            stop_price="10",
            quantity="1",
            fx_rate_to_gbp="1",
            expected_entry_cost_gbp="0",
            expected_exit_cost_gbp="0",
            adverse_allowance_gbp="0",
        )
    assert stop.value.reason == Reason.STOP_NOT_BELOW_ENTRY

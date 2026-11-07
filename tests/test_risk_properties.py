"""Deterministic properties of the Gate 1 arithmetic. Inputs are synthetic."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from sterling.risk.budget import (
    aggregate_risk_cap,
    concentration_cap,
    daily_loss_threshold_cap,
    day_loss_gbp,
    loss_exceeds_plan,
    per_trade_risk_cap,
    planned_entry_risk_gbp,
    realised_long_loss_gbp,
    round_budget_down,
    round_risk_up,
)
from sterling.risk.policy import Stage2Policy, load_default_policy
from sterling.rules.caps import daily_loss_threshold, per_trade_planned_risk
from sterling.rules.model import DeskRules

_POLICY = load_default_policy()


def _pounds(pence: int) -> Decimal:
    return (Decimal(pence) / Decimal("100")).quantize(Decimal("0.01"))


@settings(max_examples=40, deadline=None, derandomize=True)
@given(pence=st.integers(min_value=0, max_value=2_000_000))
def test_gate1_caps_stay_inside_the_stage1_ceilings(pence: int, rules: DeskRules) -> None:
    equity = _pounds(pence)
    policy: Stage2Policy = _POLICY
    per_trade = per_trade_risk_cap(equity, policy)
    daily = daily_loss_threshold_cap(equity, policy)
    assert Decimal("0.00") <= per_trade <= per_trade_planned_risk(equity, rules) <= Decimal("2.50")
    assert Decimal("0.00") <= daily <= daily_loss_threshold(equity, rules) <= Decimal("5.00")
    assert aggregate_risk_cap(equity, policy) <= equity * Decimal("0.01")
    assert concentration_cap(equity, policy) == round_budget_down(equity * Decimal("0.25"))
    assert per_trade == round_budget_down(min(equity * Decimal("0.005"), Decimal("2.50")))
    assert daily == round_budget_down(min(equity * Decimal("0.01"), Decimal("5.00")))


@settings(max_examples=30, deadline=None, derandomize=True)
@given(
    left=st.integers(min_value=0, max_value=500_000),
    right=st.integers(min_value=0, max_value=500_000),
)
def test_caps_are_monotonic(left: int, right: int) -> None:
    low = _pounds(min(left, right))
    high = _pounds(max(left, right))
    policy = _POLICY
    assert per_trade_risk_cap(low, policy) <= per_trade_risk_cap(high, policy)
    assert daily_loss_threshold_cap(low, policy) <= daily_loss_threshold_cap(high, policy)
    assert concentration_cap(low, policy) <= concentration_cap(high, policy)


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    loss_pence=st.integers(min_value=0, max_value=50_000),
    flow_pence=st.integers(min_value=0, max_value=50_000),
)
def test_external_flow_does_not_change_day_loss(loss_pence: int, flow_pence: int) -> None:
    opening = Decimal("500.00")
    loss = _pounds(loss_pence)
    equity = opening - loss
    base = day_loss_gbp(opening, equity, Decimal("0.00"))
    flow = _pounds(flow_pence)
    assert day_loss_gbp(opening, equity + flow, flow) == base
    withdraw = min(flow, equity)
    assert day_loss_gbp(opening, equity - withdraw, -withdraw) == base


@settings(max_examples=30, deadline=None, derandomize=True)
@given(
    whole=st.integers(min_value=0, max_value=1_000),
    extra=st.integers(min_value=0, max_value=99_999),
)
def test_rounding_directions_are_conservative(whole: int, extra: int) -> None:
    amount = Decimal(whole) + (Decimal(extra) / Decimal("100000"))
    assert round_budget_down(amount) <= amount <= round_risk_up(amount)
    assert round_budget_down(amount) == round_budget_down(round_budget_down(amount))
    assert round_risk_up(amount) == round_risk_up(round_risk_up(amount))


@settings(max_examples=20, deadline=None, derandomize=True)
@given(gap=st.integers(min_value=1, max_value=50))
def test_a_gap_through_the_stop_is_not_bounded_by_the_plan(gap: int) -> None:
    planned = planned_entry_risk_gbp(
        entry_price="10",
        stop_price="9",
        quantity="1",
        fx_rate_to_gbp="1",
        expected_entry_cost_gbp="0",
        expected_exit_cost_gbp="0",
        adverse_allowance_gbp="0",
    )
    exit_price = Decimal("9") - (Decimal(gap) / Decimal("100"))
    realised = realised_long_loss_gbp(
        entry_price="10",
        exit_price=exit_price,
        quantity="1",
        fx_rate_to_gbp="1",
        entry_cost_gbp="0",
        exit_cost_gbp="0",
        slippage_gbp="0",
    )
    assert loss_exceeds_plan(planned, realised) is True

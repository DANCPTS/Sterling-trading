"""Small deterministic property checks. Inputs are synthetic integers, not prices."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sterling.errors import NegativeCashRejected
from sterling.ledger.events import FillEvent
from sterling.ledger.ledger import Ledger
from sterling.ledger.money import quantize_money
from sterling.rules.caps import daily_loss_threshold, per_trade_planned_risk
from sterling.rules.model import DeskRules
from tests.support import deposit, fee, june, withdraw


def _pounds(pence: int) -> Decimal:
    return (Decimal(pence) / Decimal("100")).quantize(Decimal("0.01"))


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    loss_pence=st.integers(min_value=1, max_value=20_000),
    deposit_pence=st.integers(min_value=1, max_value=50_000),
    withdraw_pence=st.integers(min_value=0, max_value=50_000),
)
def test_external_flows_do_not_change_day_loss(
    loss_pence: int,
    deposit_pence: int,
    withdraw_pence: int,
    rules: DeskRules,
) -> None:
    loss = _pounds(loss_pence)
    incoming = _pounds(deposit_pence)
    ledger = Ledger("500.00")
    fee(ledger, event_id="fee-loss", timestamp=june(1, 9), fee_type="commission", amount=format(loss, "f"))
    before = ledger.daily_metrics(june(1, 9), rules)
    deposit(ledger, "dep-1", june(1, 10), format(incoming, "f"))
    after_deposit = ledger.daily_metrics(june(1, 10), rules)
    assert after_deposit.day_loss_gbp == before.day_loss_gbp
    assert after_deposit.day_opening_equity_gbp == Decimal("500.00")
    cash = Decimal("500.00") - loss + incoming
    outgoing = min(_pounds(withdraw_pence), cash)
    if outgoing <= 0:
        return
    withdraw(ledger, "wd-1", june(1, 11), format(outgoing, "f"))
    after_withdrawal = ledger.daily_metrics(june(1, 11), rules)
    assert after_withdrawal.day_loss_gbp == before.day_loss_gbp
    assert after_withdrawal.day_opening_equity_gbp == Decimal("500.00")


@settings(max_examples=40, deadline=None, derandomize=True)
@given(left=st.integers(min_value=0, max_value=1_000_000), right=st.integers(min_value=0, max_value=1_000_000))
def test_caps_are_bounded_and_monotonic(left: int, right: int, rules: DeskRules) -> None:
    low = _pounds(min(left, right))
    high = _pounds(max(left, right))
    per_low = per_trade_planned_risk(low, rules)
    per_high = per_trade_planned_risk(high, rules)
    daily_low = daily_loss_threshold(low, rules)
    daily_high = daily_loss_threshold(high, rules)
    assert per_low <= per_high <= Decimal("2.50")
    assert daily_low <= daily_high <= Decimal("5.00")
    assert per_low >= Decimal("0.00")
    assert daily_low >= Decimal("0.00")


@settings(max_examples=30, deadline=None, derandomize=True)
@given(st.lists(st.integers(min_value=1, max_value=10_000), min_size=1, max_size=8))
def test_deposit_replay_matches_the_sum(amounts: list[int]) -> None:
    ledger = Ledger("0.00")
    running = Decimal("0.00")
    for index, pence in enumerate(amounts):
        amount = _pounds(pence)
        deposit(ledger, f"dep-{index}", june(1, 9, index), format(amount, "f"))
        running += amount
    assert ledger.view().cash_gbp == running
    replayed = Ledger.from_events(ledger.initial_cash_gbp, ledger.events)
    assert replayed.view() == ledger.view()


@settings(max_examples=20, deadline=None, derandomize=True)
@given(extra=st.integers(min_value=1, max_value=50))
def test_buy_above_cash_never_posts_the_settlement(extra: int) -> None:
    ledger = Ledger("100.00")
    quantity = Decimal(100 + extra)
    with pytest.raises(NegativeCashRejected):
        ledger.post(FillEvent("buy-over", june(1, 10), "SYNTH-EQ", "buy", quantity, "1.00", "GBP"))
    assert ledger.events == ()
    assert ledger.view().cash_gbp == Decimal("100.00")
    assert ledger.view().positions == ()


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    whole=st.integers(min_value=-10_000, max_value=10_000),
    extra=st.integers(min_value=0, max_value=99_999),
)
def test_pence_rounding_is_idempotent(whole: int, extra: int) -> None:
    amount = Decimal(whole) + (Decimal(extra) / Decimal("100000"))
    once = quantize_money(amount)
    assert quantize_money(once) == once
    assert abs(once - amount) <= Decimal("0.005")

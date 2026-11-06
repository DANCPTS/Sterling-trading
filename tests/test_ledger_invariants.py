"""Accounting invariants: cash, inventory, idempotency, and corrections."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sterling.errors import (
    IdempotencyConflict,
    InvalidEvent,
    NegativeCashRejected,
    OversellRejected,
    ReversalRejected,
)
from sterling.ledger.events import DepositEvent, FillEvent, FxEvent
from sterling.ledger.ledger import Ledger
from sterling.rules.model import DeskRules
from tests.support import deposit, fee, june, mark, reversal, settle


def test_duplicate_event_is_idempotent() -> None:
    ledger = Ledger("500.00")
    event = DepositEvent("dep-1", june(1, 9), "25.00")
    ledger.post(event)
    ledger.post(DepositEvent("dep-1", june(1, 9), "25.00"))
    view = ledger.view()
    assert view.cash_gbp == Decimal("525.00")
    assert view.event_ids.count("dep-1") == 1


def test_same_id_with_a_different_payload_conflicts() -> None:
    ledger = Ledger("500.00")
    ledger.post(DepositEvent("dep-1", june(1, 9), "25.00"))
    with pytest.raises(IdempotencyConflict):
        ledger.post(DepositEvent("dep-1", june(1, 9), "30.00"))
    assert ledger.view().cash_gbp == Decimal("525.00")


def test_buy_that_would_make_cash_negative_is_rejected_and_leaves_the_log() -> None:
    ledger = Ledger("500.00")
    ledger.post(FillEvent("buy-too-big", june(1, 10), "SYNTH-EQ", "buy", "600", "1.00", "GBP"))
    before = ledger.events
    with pytest.raises(NegativeCashRejected):
        ledger.post(FxEvent("fx-too-big", june(1, 10, 1), "buy-too-big", "1"))
    assert ledger.events == before
    assert ledger.view().cash_gbp == Decimal("500.00")
    assert ledger.view().positions == ()


def test_rejected_first_settlement_does_not_invent_a_baseline_beyond_the_fill() -> None:
    ledger = Ledger("100.00")
    with pytest.raises(NegativeCashRejected):
        settle(
            ledger,
            event_id="buy-over",
            timestamp=june(1, 10),
            instrument_id="SYNTH-EQ",
            side="buy",
            quantity="101",
            price="1.00",
        )
    # The fill was accepted before the FX rejection. Cash is unchanged and nothing is held.
    assert ledger.view().cash_gbp == Decimal("100.00")
    assert ledger.view().positions == ()
    assert "buy-over" in ledger.view().pending_fill_ids


def test_oversell_is_rejected() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="1.00",
    )
    before = ledger.events
    ledger.post(FillEvent("sell-too-many", june(1, 11), "SYNTH-EQ", "sell", "3", "1.00", "GBP"))
    with pytest.raises(OversellRejected):
        ledger.post(FxEvent("fx-sell-too-many", june(1, 11), "sell-too-many", "1"))
    assert ledger.events[-1].event_id == "sell-too-many"
    assert ledger.view().positions[0].quantity == Decimal("2")
    assert ledger.view().cash_gbp == Decimal("498.00")
    assert len(ledger.events) == len(before) + 1


def test_cash_may_reach_zero() -> None:
    ledger = Ledger("10.00")
    settle(
        ledger,
        event_id="buy-all",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="10.00",
    )
    assert ledger.view().cash_gbp == Decimal("0.00")
    with pytest.raises(NegativeCashRejected):
        fee(ledger, event_id="fee-after", timestamp=june(1, 11), fee_type="commission", amount="0.01")
    assert ledger.view().cash_gbp == Decimal("0.00")


def test_gbp_fx_rate_must_be_one() -> None:
    ledger = Ledger("500.00")
    ledger.post(FillEvent("buy-gbp", june(1, 10), "SYNTH-EQ", "buy", "1", "10.00", "GBP"))
    with pytest.raises(InvalidEvent, match="fx_rate_to_gbp of 1"):
        ledger.post(FxEvent("fx-bad-gbp", june(1, 10, 1), "buy-gbp", "0.99"))
    assert ledger.view().cash_gbp == Decimal("500.00")


def test_reversal_replaces_a_wrong_fx_rate() -> None:
    # Wrong settlement: 1 * 10 USD * 0.80 = 8.00. Correct rate 0.79 → 7.90.
    ledger = Ledger("500.00")
    ledger.post(FillEvent("buy-usd", june(1, 10), "SYNTH-US", "buy", "1", "10", "USD"))
    ledger.post(FxEvent("fx-bad", june(1, 10, 1), "buy-usd", "0.80"))
    assert ledger.view().cash_gbp == Decimal("492.00")
    reversal(ledger, "rev-fx", june(1, 10, 2), "fx-bad", "wrong caller rate")
    assert ledger.view().cash_gbp == Decimal("500.00")
    assert ledger.view().positions == ()
    assert "buy-usd" in ledger.view().pending_fill_ids
    ledger.post(FxEvent("fx-good", june(1, 10, 3), "buy-usd", "0.79"))
    view = ledger.view()
    assert view.cash_gbp == Decimal("492.10")
    assert view.positions[0].total_cost_gbp == Decimal("7.90")
    assert "fx-bad" not in view.event_ids or "fx-bad" in view.event_ids
    assert "rev-fx" in view.event_ids


def test_snapshot_cannot_be_reversed() -> None:
    ledger = Ledger("500.00")
    ledger.capture_day_baseline(june(1, 8))
    with pytest.raises(ReversalRejected):
        reversal(ledger, "rev-base", june(1, 8, 1), "day-baseline-2026-06-01", "not allowed")
    assert ledger.view().baselines == (("2026-06-01", Decimal("500.00")),)


def test_reversing_a_buy_after_it_was_sold_is_rejected_until_the_sell_is_reversed() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    settle(
        ledger,
        event_id="sell-1",
        timestamp=june(1, 11),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="2",
        price="10.00",
    )
    with pytest.raises(ReversalRejected):
        reversal(ledger, "rev-buy", june(1, 12), "fx-buy-1", "too late")
    assert ledger.view().cash_gbp == Decimal("500.00")
    reversal(ledger, "rev-sell", june(1, 12, 1), "fx-sell-1", "undo sale first")
    assert ledger.view().positions[0].quantity == Decimal("2")
    reversal(ledger, "rev-buy", june(1, 12, 2), "fx-buy-1", "undo purchase")
    assert ledger.view().cash_gbp == Decimal("500.00")
    assert ledger.view().positions == ()


def test_replay_matches_the_posted_ledger() -> None:
    from sterling.rules.loader import load_default_rules

    rules = load_default_rules()
    ledger = Ledger.open_primary_account(rules)
    deposit(ledger, "dep-1", june(1, 9), "25.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    replayed = Ledger.from_events(
        ledger.initial_cash_gbp,
        ledger.events,
        rules_version_id=ledger.rules_version_id,
        rules_content_hash=ledger.rules_content_hash,
    )
    assert replayed.view() == ledger.view()
    assert replayed.events == ledger.events


def test_open_primary_account_is_not_an_external_deposit() -> None:
    from sterling.ledger.events import DepositEvent, WithdrawalEvent
    from sterling.rules.loader import load_default_rules

    rules = load_default_rules()
    ledger = Ledger.open_primary_account(rules)
    ledger.capture_day_baseline(june(1, 8))
    metrics = ledger.daily_metrics(june(1, 8), rules)
    assert ledger.view().cash_gbp == Decimal("500.00")
    assert metrics.day_opening_equity_gbp == Decimal("500.00")
    assert metrics.net_external_deposits_gbp == Decimal("0.00")
    assert not any(isinstance(event, (DepositEvent, WithdrawalEvent)) for event in ledger.events)


def test_fee_and_mark_reversals_restore_the_books() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    fee(ledger, event_id="fee-1", timestamp=june(1, 10, 1), fee_type="commission", amount="1.00")
    mark(ledger, event_id="mark-1", timestamp=june(1, 10, 2), instrument_id="SYNTH-EQ", price="12.00")
    assert ledger.view().cash_gbp == Decimal("479.00")
    assert ledger.view().positions[0].unrealised_pnl_gbp == Decimal("4.00")
    reversal(ledger, "rev-fee", june(1, 10, 3), "fee-1", "fee was posted in error")
    reversal(ledger, "rev-mark", june(1, 10, 4), "mark-1", "mark was posted in error")
    view = ledger.view()
    assert view.cash_gbp == Decimal("480.00")
    assert view.fees_gbp == (
        ("commission", Decimal("0.00")),
        ("fx_fee", Decimal("0.00")),
        ("stamp_duty", Decimal("0.00")),
    )
    assert view.positions[0].mark_price is None
    assert view.positions[0].unrealised_pnl_gbp == Decimal("0.00")
    assert view.equity_gbp == Decimal("500.00")


def test_reversed_deposit_drops_out_of_the_external_flow(rules: DeskRules) -> None:
    ledger = Ledger("500.00")
    deposit(ledger, "dep-1", june(1, 9), "100.00")
    fee(ledger, event_id="fee-1", timestamp=june(1, 10), fee_type="commission", amount="1.00")
    reversal(ledger, "rev-dep", june(1, 11), "dep-1", "deposit was not external")
    metrics = ledger.daily_metrics(june(1, 11), rules)
    assert metrics.day_opening_equity_gbp == Decimal("500.00")
    assert metrics.net_external_deposits_gbp == Decimal("0.00")
    assert metrics.marked_equity_gbp == Decimal("499.00")
    assert metrics.day_loss_gbp == Decimal("1.00")


def test_reversing_a_spent_deposit_is_rejected() -> None:
    ledger = Ledger("0.00")
    deposit(ledger, "dep-1", june(1, 9), "10.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="10.00",
    )
    with pytest.raises(NegativeCashRejected):
        reversal(ledger, "rev-dep", june(1, 11), "dep-1", "cash already spent")
    assert ledger.view().cash_gbp == Decimal("0.00")
    assert ledger.view().positions[0].quantity == Decimal("1")


def test_instrument_currency_cannot_change() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-gbp",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="10.00",
    )
    with pytest.raises(InvalidEvent, match="currency"):
        ledger.post(FillEvent("buy-usd", june(1, 11), "SYNTH-EQ", "buy", "1", "10", "USD"))
    assert ledger.view().positions[0].currency == "GBP"


def test_second_fx_on_a_settled_fill_is_rejected() -> None:
    ledger = Ledger("500.00")
    ledger.post(FillEvent("buy-1", june(1, 10), "SYNTH-EQ", "buy", "1", "10.00", "GBP"))
    ledger.post(FxEvent("fx-1", june(1, 10, 1), "buy-1", "1"))
    with pytest.raises(InvalidEvent, match="already settled"):
        ledger.post(FxEvent("fx-2", june(1, 10, 2), "buy-1", "1"))
    assert ledger.view().cash_gbp == Decimal("490.00")


def test_reserved_baseline_id_cannot_be_used_for_a_deposit() -> None:
    ledger = Ledger("500.00")
    with pytest.raises(InvalidEvent, match="reserved"):
        ledger.post(DepositEvent("day-baseline-2026-06-01", june(1, 9), "1.00"))
    assert ledger.events == ()


def test_from_events_rejects_a_duplicate_id() -> None:
    ledger = Ledger("500.00")
    deposit(ledger, "dep-1", june(1, 9), "1.00")
    duplicated = (*ledger.events, ledger.events[-1])
    with pytest.raises(IdempotencyConflict):
        Ledger.from_events("500.00", duplicated)

"""Day-loss formula. Enforcement of the threshold is a later stage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sterling.ledger.ledger import Ledger
from sterling.rules.model import DeskRules
from tests.support import deposit, fee, june, mark, settle, withdraw


def test_day_loss_is_zero_until_a_baseline_exists(rules: DeskRules) -> None:
    ledger = Ledger("500.00")
    metrics = ledger.daily_metrics(june(1, 8), rules)
    assert metrics.baseline_established is False
    assert metrics.day_opening_equity_gbp is None
    assert metrics.marked_equity_gbp == Decimal("500.00")
    assert metrics.day_loss_gbp == Decimal("0.00")
    assert metrics.daily_loss_threshold_reached is None


def test_loss_then_deposit_and_withdrawal_do_not_reset_the_budget(rules: DeskRules) -> None:
    # Buy 10 @ 10, mark at 9. Equity 490. Day loss 10, which is above the 5.00 threshold.
    # Deposit 100 and withdraw 20 leave adjusted equity at 490 and the opening print at 500.
    ledger = Ledger.open_primary_account(rules)
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="10",
        price="10.00",
    )
    mark(ledger, event_id="mark-down", timestamp=june(1, 11), instrument_id="SYNTH-EQ", price="9.00")
    opened = ledger.daily_metrics(june(1, 11), rules)
    assert opened.day_opening_equity_gbp == Decimal("500.00")
    assert opened.marked_equity_gbp == Decimal("490.00")
    assert opened.day_loss_gbp == Decimal("10.00")
    assert opened.daily_loss_threshold_gbp == Decimal("5.00")
    assert opened.daily_loss_threshold_reached is True

    deposit(ledger, "dep-1", june(1, 12), "100.00")
    after_deposit = ledger.daily_metrics(june(1, 12), rules)
    assert after_deposit.day_opening_equity_gbp == Decimal("500.00")
    assert after_deposit.marked_equity_gbp == Decimal("590.00")
    assert after_deposit.net_external_deposits_gbp == Decimal("100.00")
    assert after_deposit.adjusted_equity_gbp == Decimal("490.00")
    assert after_deposit.day_loss_gbp == Decimal("10.00")

    withdraw(ledger, "wd-1", june(1, 13), "20.00")
    after_withdrawal = ledger.daily_metrics(june(1, 13), rules)
    assert after_withdrawal.day_opening_equity_gbp == Decimal("500.00")
    assert after_withdrawal.marked_equity_gbp == Decimal("570.00")
    assert after_withdrawal.net_external_deposits_gbp == Decimal("80.00")
    assert after_withdrawal.adjusted_equity_gbp == Decimal("490.00")
    assert after_withdrawal.day_loss_gbp == Decimal("10.00")

    # Stage 1 records the breach and still accepts another synthetic fill.
    settle(
        ledger,
        event_id="buy-2",
        timestamp=june(1, 14),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="1.00",
    )
    assert ledger.view().cash_gbp == Decimal("479.00")


def test_withdrawal_alone_is_not_a_trading_loss(rules: DeskRules) -> None:
    ledger = Ledger("500.00")
    withdraw(ledger, "wd-1", june(1, 10), "10.00")
    metrics = ledger.daily_metrics(june(1, 10), rules)
    assert metrics.day_opening_equity_gbp == Decimal("500.00")
    assert metrics.marked_equity_gbp == Decimal("490.00")
    assert metrics.net_external_deposits_gbp == Decimal("-10.00")
    assert metrics.adjusted_equity_gbp == Decimal("500.00")
    assert metrics.day_loss_gbp == Decimal("0.00")
    assert metrics.daily_loss_threshold_reached is False


def test_mark_recovery_reduces_point_in_time_day_loss(rules: DeskRules) -> None:
    # The formula is not an intra-day high-water mark.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="10",
        price="10.00",
    )
    mark(ledger, event_id="mark-9", timestamp=june(1, 11), instrument_id="SYNTH-EQ", price="9.00")
    assert ledger.daily_metrics(june(1, 11), rules).day_loss_gbp == Decimal("10.00")
    mark(ledger, event_id="mark-9-50", timestamp=june(1, 12), instrument_id="SYNTH-EQ", price="9.50")
    recovered = ledger.daily_metrics(june(1, 12), rules)
    assert recovered.marked_equity_gbp == Decimal("495.00")
    assert recovered.day_loss_gbp == Decimal("5.00")
    assert recovered.day_opening_equity_gbp == Decimal("500.00")


def test_fee_reduces_equity_once(rules: DeskRules) -> None:
    ledger = Ledger("500.00")
    fee(ledger, event_id="fee-1", timestamp=june(1, 10), fee_type="commission", amount="1.00")
    metrics = ledger.daily_metrics(june(1, 10), rules)
    assert ledger.view().equity_gbp == Decimal("499.00")
    assert ledger.view().fees_gbp[0] == ("commission", Decimal("1.00"))
    assert metrics.day_loss_gbp == Decimal("1.00")
    assert metrics.marked_equity_gbp != Decimal("498.00")


def test_later_day_does_not_reuse_or_reset_the_previous_baseline(rules: DeskRules) -> None:
    ledger = Ledger("500.00")
    deposit(ledger, "dep-1", june(1, 10), "100.00")
    day_one = ledger.daily_metrics(june(1, 10), rules)
    assert day_one.day_opening_equity_gbp == Decimal("500.00")
    assert day_one.adjusted_equity_gbp == Decimal("500.00")
    assert day_one.day_loss_gbp == Decimal("0.00")

    fee(ledger, event_id="fee-next", timestamp=june(2, 10), fee_type="commission", amount="1.00")
    day_two = ledger.daily_metrics(june(2, 10), rules)
    assert day_two.day_opening_equity_gbp == Decimal("600.00")
    assert day_two.marked_equity_gbp == Decimal("599.00")
    assert day_two.net_external_deposits_gbp == Decimal("0.00")
    assert day_two.day_loss_gbp == Decimal("1.00")
    assert ledger.daily_metrics(june(1, 10), rules) == day_one


def test_second_capture_on_the_same_day_does_not_move_the_baseline(rules: DeskRules) -> None:
    ledger = Ledger("500.00")
    ledger.capture_day_baseline(june(1, 8))
    fee(ledger, event_id="fee-1", timestamp=june(1, 9), fee_type="commission", amount="1.50")
    ledger.capture_day_baseline(june(1, 15))
    metrics = ledger.daily_metrics(june(1, 15), rules)
    assert metrics.day_opening_equity_gbp == Decimal("500.00")
    assert metrics.day_loss_gbp == Decimal("1.50")
    assert ledger.view().baselines == (("2026-06-01", Decimal("500.00")),)


def test_naive_timestamp_is_rejected() -> None:
    import pytest

    from sterling.errors import InvalidEvent
    from sterling.ledger.events import DepositEvent

    ledger = Ledger("500.00")
    with pytest.raises(InvalidEvent):
        ledger.post(DepositEvent("dep-naive", datetime(2026, 6, 1, 10, 0), "1.00"))


def test_loss_streak_is_counted_and_not_enforced() -> None:
    # Three losing round trips, a flat one that does not reset the streak,
    # a fourth loss that is still accepted, then a winner that resets it.
    ledger = Ledger("500.00")
    minute = 0

    def at(hour: int = 10) -> datetime:
        nonlocal minute
        stamp = june(1, hour, minute)
        minute += 1
        return stamp

    def round_trip(name: str, sell_price: str) -> None:
        settle(
            ledger,
            event_id=f"buy-{name}",
            timestamp=at(),
            instrument_id="SYNTH-EQ",
            side="buy",
            quantity="1",
            price="10.00",
        )
        settle(
            ledger,
            event_id=f"sell-{name}",
            timestamp=at(),
            instrument_id="SYNTH-EQ",
            side="sell",
            quantity="1",
            price=sell_price,
        )

    round_trip("l1", "9.00")
    assert ledger.consecutive_net_closed_losses() == 1
    round_trip("flat", "10.00")
    assert ledger.consecutive_net_closed_losses() == 1
    round_trip("l2", "9.00")
    round_trip("l3", "9.00")
    assert ledger.consecutive_net_closed_losses() == 3
    round_trip("l4", "9.00")
    assert ledger.consecutive_net_closed_losses() == 4
    round_trip("win", "11.00")
    assert ledger.consecutive_net_closed_losses() == 0

    from tests.support import reversal

    reversal(ledger, "rev-win", at(), "fx-sell-win", "undo the winning sale")
    assert ledger.consecutive_net_closed_losses() == 4


def test_fee_on_a_winning_sell_can_make_the_close_a_net_loss() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(5, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="10.00",
    )
    settle(
        ledger,
        event_id="sell-1",
        timestamp=june(5, 11),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="1",
        price="10.50",
    )
    assert ledger.consecutive_net_closed_losses() == 0
    fee(
        ledger,
        event_id="fee-sell",
        timestamp=june(5, 11, 1),
        fee_type="commission",
        amount="1.00",
        related_event_id="sell-1",
    )
    assert ledger.consecutive_net_closed_losses() == 1
    assert ledger.view().realised_pnl_gbp == Decimal("0.50")


def test_buy_fee_does_not_open_a_loss_streak() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(5, 12),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="10.00",
    )
    fee(
        ledger,
        event_id="fee-buy",
        timestamp=june(5, 12, 1),
        fee_type="commission",
        amount="1.00",
        related_event_id="buy-1",
    )
    assert ledger.consecutive_net_closed_losses() == 0


def test_threshold_uses_day_opening_equity_not_a_later_deposit(rules: DeskRules) -> None:
    ledger = Ledger("100.00")
    ledger.capture_day_baseline(june(1, 8))
    deposit(ledger, "dep-1", june(1, 9), "400.00")
    metrics = ledger.daily_metrics(june(1, 9), rules)
    assert metrics.day_opening_equity_gbp == Decimal("100.00")
    assert metrics.daily_loss_threshold_gbp == Decimal("1.00")
    assert metrics.day_loss_gbp == Decimal("0.00")


def test_timezone_aware_comparison_uses_the_instant() -> None:
    # 11:00 UTC+1 and 10:00 UTC are the same instant and must compare equal for idempotency.
    bst = june(1, 11)
    utc = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    assert bst == utc
    assert bst - utc == timedelta(0)

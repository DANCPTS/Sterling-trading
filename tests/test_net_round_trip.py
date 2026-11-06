"""Hand-calculated net round trips. Instruments and prices are synthetic.

Entry fees are not added to average cost. Equity therefore drops by each fee
once, through cash. The loss streak uses the allocated net, which can be a
loss when the price result is a gain.
"""

from __future__ import annotations

from decimal import Decimal

from sterling.ledger.ledger import Ledger
from tests.support import fee, june, settle


def test_partial_exits_allocate_entry_fees_and_keep_the_remainder() -> None:
    # Buys: 4 @ 10 = 40 and 6 @ 12 = 72. Price cost = 112.
    # Entry fees: 0.40 + 0.20 + 0.30 + 0.36 = 1.26, pooled, not capitalised.
    # Sell 3 @ 13. Cost relieved = round(112 * 3 / 10) = 33.60. Price P&L = 5.40.
    # Entry fees relieved = round(1.26 * 3 / 10) = 0.38. Exit fee 0.15.
    # Net = 5.40 - 0.38 - 0.15 = 4.87. Pool left = 0.88 on 7 shares.
    # Sell 2.5 @ 11. Cost relieved = 78.40 * 2.5 / 7 = 28.00. Price P&L = -0.50.
    # Entry fees relieved = round(0.88 * 2.5 / 7) = 0.31. Exit fee 0.10.
    # Net = -0.91. Pool left = 0.57 on 4.5 shares.
    # Close 4.5 @ 12.50. Cost relieved = 50.40. Price P&L = 5.85.
    # Entry fees relieved = the exact remainder 0.57. Exit fees 0.20 + 0.05 = 0.25.
    # Net = 5.03.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(1, 10, 0),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="4",
        price="10.00",
    )
    fee(ledger, event_id="fee-a-c", timestamp=june(1, 10, 1), fee_type="commission", amount="0.40", related_event_id="buy-a")
    fee(ledger, event_id="fee-a-s", timestamp=june(1, 10, 2), fee_type="stamp_duty", amount="0.20", related_event_id="buy-a")
    settle(
        ledger,
        event_id="buy-b",
        timestamp=june(1, 10, 3),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="6",
        price="12.00",
    )
    fee(ledger, event_id="fee-b-c", timestamp=june(1, 10, 4), fee_type="commission", amount="0.30", related_event_id="buy-b")
    fee(
        ledger,
        event_id="fee-b-s",
        timestamp=june(1, 10, 5),
        fee_type="stamp_duty",
        amount="0.36",
        related_event_id="fx-buy-b",
    )
    held = ledger.view()
    assert held.positions[0].total_cost_gbp == Decimal("112.00")
    assert held.positions[0].unallocated_entry_fees_gbp == Decimal("1.26")
    assert held.cash_gbp == Decimal("386.74")
    assert held.equity_gbp == Decimal("498.74")

    settle(
        ledger,
        event_id="sell-1",
        timestamp=june(1, 11, 0),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="3",
        price="13.00",
    )
    fee(ledger, event_id="fee-s1", timestamp=june(1, 11, 1), fee_type="commission", amount="0.15", related_event_id="sell-1")
    first = ledger.closed_trades()[0]
    assert first.price_pnl_gbp == Decimal("5.40")
    assert first.allocated_entry_fees_gbp == Decimal("0.38")
    assert first.exit_fees_gbp == Decimal("0.15")
    assert first.net_gbp == Decimal("4.87")
    assert ledger.view().positions[0].quantity == Decimal("7")
    assert ledger.view().positions[0].unallocated_entry_fees_gbp == Decimal("0.88")
    assert ledger.consecutive_net_closed_losses() == 0

    settle(
        ledger,
        event_id="sell-2",
        timestamp=june(1, 12, 0),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="2.5",
        price="11.00",
    )
    fee(ledger, event_id="fee-s2", timestamp=june(1, 12, 1), fee_type="stamp_duty", amount="0.10", related_event_id="sell-2")
    second = ledger.closed_trades()[1]
    assert second.price_pnl_gbp == Decimal("-0.50")
    assert second.allocated_entry_fees_gbp == Decimal("0.31")
    assert second.exit_fees_gbp == Decimal("0.10")
    assert second.net_gbp == Decimal("-0.91")
    assert ledger.view().positions[0].unallocated_entry_fees_gbp == Decimal("0.57")
    assert ledger.view().positions[0].total_cost_gbp == Decimal("50.40")
    assert ledger.consecutive_net_closed_losses() == 1

    settle(
        ledger,
        event_id="sell-3",
        timestamp=june(1, 13, 0),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="4.5",
        price="12.50",
    )
    fee(ledger, event_id="fee-s3-c", timestamp=june(1, 13, 1), fee_type="commission", amount="0.20", related_event_id="sell-3")
    fee(ledger, event_id="fee-s3-x", timestamp=june(1, 13, 2), fee_type="fx_fee", amount="0.05", related_event_id="fx-sell-3")
    third = ledger.closed_trades()[2]
    assert third.price_pnl_gbp == Decimal("5.85")
    assert third.allocated_entry_fees_gbp == Decimal("0.57")
    assert third.exit_fees_gbp == Decimal("0.25")
    assert third.net_gbp == Decimal("5.03")
    assert ledger.view().positions == ()
    assert ledger.consecutive_net_closed_losses() == 0
    assert ledger.view().realised_pnl_gbp == Decimal("10.75")


def test_fractional_quantity_splits_the_entry_fee_pool() -> None:
    # Buy 0.5 @ 10 = 5.00, entry commission 0.10.
    # Sell 0.25 @ 12. Cost relieved 2.50. Price P&L 0.50.
    # Entry fees relieved = 0.05. Exit commission 0.03. Net = 0.42.
    # Remainder 0.05 stays on 0.25 shares.
    # Close @ 10. Price P&L 0. Entry fees 0.05. Exit 0.02. Net = -0.07.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-f",
        timestamp=june(2, 10, 0),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="0.5",
        price="10.00",
    )
    fee(ledger, event_id="fee-in", timestamp=june(2, 10, 1), fee_type="commission", amount="0.10", related_event_id="buy-f")
    settle(
        ledger,
        event_id="sell-f",
        timestamp=june(2, 11, 0),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="0.25",
        price="12.00",
    )
    fee(ledger, event_id="fee-out", timestamp=june(2, 11, 1), fee_type="commission", amount="0.03", related_event_id="sell-f")
    first = ledger.closed_trades()[0]
    assert first.quantity == Decimal("0.25")
    assert first.price_pnl_gbp == Decimal("0.50")
    assert first.allocated_entry_fees_gbp == Decimal("0.05")
    assert first.exit_fees_gbp == Decimal("0.03")
    assert first.net_gbp == Decimal("0.42")
    assert ledger.view().positions[0].unallocated_entry_fees_gbp == Decimal("0.05")
    assert ledger.view().positions[0].total_cost_gbp == Decimal("2.50")

    settle(
        ledger,
        event_id="sell-g",
        timestamp=june(2, 12, 0),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="0.25",
        price="10.00",
    )
    fee(ledger, event_id="fee-out-2", timestamp=june(2, 12, 1), fee_type="commission", amount="0.02", related_event_id="fx-sell-g")
    second = ledger.closed_trades()[1]
    assert second.price_pnl_gbp == Decimal("0.00")
    assert second.allocated_entry_fees_gbp == Decimal("0.05")
    assert second.exit_fees_gbp == Decimal("0.02")
    assert second.net_gbp == Decimal("-0.07")
    assert ledger.consecutive_net_closed_losses() == 1


def test_fx_charges_on_a_non_gbp_instrument_join_the_pool() -> None:
    # 4 * 10 USD * 0.80 = 32.00. Entry fx fee 0.40 and commission 0.16.
    # Sell 4 * 10.50 USD * 0.80 = 33.60. Price P&L = 1.60.
    # Full close takes both entry fees. Exit fx fee 0.20.
    # Net = 1.60 - 0.56 - 0.20 = 0.84.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-usd",
        timestamp=june(3, 10, 0),
        instrument_id="SYNTH-US",
        side="buy",
        quantity="4",
        price="10",
        currency="USD",
        fx_rate="0.80",
    )
    fee(ledger, event_id="fee-fx-in", timestamp=june(3, 10, 1), fee_type="fx_fee", amount="0.40", related_event_id="fx-buy-usd")
    fee(ledger, event_id="fee-c-in", timestamp=june(3, 10, 2), fee_type="commission", amount="0.16", related_event_id="buy-usd")
    assert ledger.view().positions[0].total_cost_gbp == Decimal("32.00")
    assert ledger.view().positions[0].unallocated_entry_fees_gbp == Decimal("0.56")
    assert ledger.view().equity_gbp == Decimal("499.44")

    settle(
        ledger,
        event_id="sell-usd",
        timestamp=june(3, 11, 0),
        instrument_id="SYNTH-US",
        side="sell",
        quantity="4",
        price="10.50",
        currency="USD",
        fx_rate="0.80",
    )
    fee(ledger, event_id="fee-fx-out", timestamp=june(3, 11, 1), fee_type="fx_fee", amount="0.20", related_event_id="fx-sell-usd")
    trade = ledger.closed_trades()[0]
    assert trade.price_pnl_gbp == Decimal("1.60")
    assert trade.allocated_entry_fees_gbp == Decimal("0.56")
    assert trade.exit_fees_gbp == Decimal("0.20")
    assert trade.net_gbp == Decimal("0.84")
    assert ledger.consecutive_net_closed_losses() == 0
    assert ledger.view().equity_gbp == Decimal("500.84")


def test_price_gain_is_a_net_loss_after_entry_and_exit_costs() -> None:
    # 1 * 10 USD * 0.80 = 8.00. Entry fees 0.30 + 0.10 + 0.25 = 0.65.
    # Sell 1 * 10.20 USD * 0.80 = 8.16. Price P&L = 0.16, before costs a gain.
    # Exit commission 0.10. Net = 0.16 - 0.65 - 0.10 = -0.59, a loss.
    # Cash ends at 500 - 0.59 = 499.41, so the fees were not counted twice.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-usd",
        timestamp=june(4, 10, 0),
        instrument_id="SYNTH-US",
        side="buy",
        quantity="1",
        price="10",
        currency="USD",
        fx_rate="0.80",
    )
    fee(ledger, event_id="fee-c", timestamp=june(4, 10, 1), fee_type="commission", amount="0.30", related_event_id="buy-usd")
    fee(ledger, event_id="fee-s", timestamp=june(4, 10, 2), fee_type="stamp_duty", amount="0.10", related_event_id="buy-usd")
    fee(ledger, event_id="fee-x", timestamp=june(4, 10, 3), fee_type="fx_fee", amount="0.25", related_event_id="fx-buy-usd")
    settle(
        ledger,
        event_id="sell-usd",
        timestamp=june(4, 11, 0),
        instrument_id="SYNTH-US",
        side="sell",
        quantity="1",
        price="10.20",
        currency="USD",
        fx_rate="0.80",
    )
    fee(ledger, event_id="fee-out", timestamp=june(4, 11, 1), fee_type="commission", amount="0.10", related_event_id="sell-usd")
    trade = ledger.closed_trades()[0]
    assert trade.price_pnl_gbp == Decimal("0.16")
    assert trade.price_pnl_gbp > 0
    assert trade.allocated_entry_fees_gbp == Decimal("0.65")
    assert trade.exit_fees_gbp == Decimal("0.10")
    assert trade.net_gbp == Decimal("-0.59")
    assert ledger.consecutive_net_closed_losses() == 1
    assert ledger.view().equity_gbp == Decimal("499.41")
    assert ledger.view().positions == ()

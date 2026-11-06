"""Hand-calculated synthetic ledger scenarios.

SYNTH-EQ and SYNTH-US are invented labels. None of these figures is a market
price or a profitability claim.
"""

from __future__ import annotations

from decimal import Decimal

from sterling.ledger.ledger import Ledger
from sterling.rules.model import DeskRules
from tests.support import fee, june, mark, settle


def test_partial_fills_average_cost_and_full_close() -> None:
    # Buy 2 @ 10 and 6 @ 11. Cost = 20 + 66 = 86. Average = 10.75.
    # Sell 3 @ 12. Proceeds 36. Cost relieved = round(86 * 3 / 8) = 32.25.
    # Realised 3.75. Remaining cost 53.75 on 5 shares.
    # Close 5 @ 12. Proceeds 60. Remaining cost 53.75 is relieved exactly.
    # Realised on the close 6.25. Total realised 10. Cash = 500 - 86 + 96 = 510.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(1, 10, 0),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    settle(
        ledger,
        event_id="buy-b",
        timestamp=june(1, 10, 5),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="6",
        price="11.00",
    )
    held = ledger.view().positions[0]
    assert held.quantity == Decimal("8")
    assert held.total_cost_gbp == Decimal("86.00")
    assert held.average_cost_gbp == Decimal("10.75")
    assert ledger.view().cash_gbp == Decimal("414.00")

    settle(
        ledger,
        event_id="sell-a",
        timestamp=june(1, 10, 10),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="3",
        price="12.00",
    )
    partial = ledger.view()
    assert partial.cash_gbp == Decimal("450.00")
    assert partial.realised_pnl_gbp == Decimal("3.75")
    assert partial.positions[0].quantity == Decimal("5")
    assert partial.positions[0].total_cost_gbp == Decimal("53.75")
    assert partial.positions[0].average_cost_gbp == Decimal("10.75")

    settle(
        ledger,
        event_id="sell-b",
        timestamp=june(1, 10, 15),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="5",
        price="12.00",
    )
    closed = ledger.view()
    assert closed.cash_gbp == Decimal("510.00")
    assert closed.realised_pnl_gbp == Decimal("10.00")
    assert closed.positions == ()
    assert closed.equity_gbp == Decimal("510.00")


def test_explicit_costs_mark_and_sell_at_the_mark() -> None:
    # Costs below are caller-supplied lines, not a statutory stamp calculation.
    # Cash path: 500 - 20 - 0.50 - 0.10 - 66 - 0.33 + 36 = 449.07.
    # After the mark at 12, eight shares are worth 96, so equity is 413.07 + 96 = 509.07.
    # Selling 3 at that mark moves 36 of value into cash and leaves equity unchanged.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(1, 10, 0),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    fee(
        ledger,
        event_id="fee-c1",
        timestamp=june(1, 10, 1),
        fee_type="commission",
        amount="0.50",
        related_event_id="buy-a",
    )
    fee(
        ledger,
        event_id="fee-s1",
        timestamp=june(1, 10, 2),
        fee_type="stamp_duty",
        amount="0.10",
        related_event_id="buy-a",
    )
    settle(
        ledger,
        event_id="buy-b",
        timestamp=june(1, 10, 5),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="6",
        price="11.00",
    )
    fee(
        ledger,
        event_id="fee-s2",
        timestamp=june(1, 10, 6),
        fee_type="stamp_duty",
        amount="0.33",
        related_event_id="buy-b",
    )
    mark(ledger, event_id="mark-1", timestamp=june(1, 10, 7), instrument_id="SYNTH-EQ", price="12.00")

    marked = ledger.view()
    assert marked.cash_gbp == Decimal("413.07")
    assert marked.positions[0].market_value_gbp == Decimal("96.00")
    assert marked.positions[0].unrealised_pnl_gbp == Decimal("10.00")
    assert marked.equity_gbp == Decimal("509.07")
    assert marked.fees_gbp == (
        ("commission", Decimal("0.50")),
        ("fx_fee", Decimal("0.00")),
        ("stamp_duty", Decimal("0.43")),
    )

    settle(
        ledger,
        event_id="sell-a",
        timestamp=june(1, 10, 10),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="3",
        price="12.00",
    )
    sold = ledger.view()
    assert sold.cash_gbp == Decimal("449.07")
    assert sold.realised_pnl_gbp == Decimal("3.75")
    assert sold.positions[0].quantity == Decimal("5")
    assert sold.positions[0].total_cost_gbp == Decimal("53.75")
    assert sold.positions[0].market_value_gbp == Decimal("60.00")
    assert sold.positions[0].unrealised_pnl_gbp == Decimal("6.25")
    assert sold.equity_gbp == Decimal("509.07")


def test_foreign_currency_fx_is_counted_once(rules: DeskRules) -> None:
    # 4 * 10 USD * 0.80 = 32.00 GBP notional. FX fee 0.40 is a separate line.
    # Remark at 0.75 makes market value 30.00. Equity drop is 2.00 + 0.40 = 2.40, once.
    ledger = Ledger.open_primary_account(rules)
    settle(
        ledger,
        event_id="buy-usd",
        timestamp=june(2, 11, 0),
        instrument_id="SYNTH-US",
        side="buy",
        quantity="4",
        price="10",
        currency="USD",
        fx_rate="0.80",
    )
    fee(
        ledger,
        event_id="fee-fx",
        timestamp=june(2, 11, 1),
        fee_type="fx_fee",
        amount="0.40",
        related_event_id="fx-buy-usd",
    )
    mark(
        ledger,
        event_id="mark-usd",
        timestamp=june(2, 11, 2),
        instrument_id="SYNTH-US",
        price="10",
        fx_rate="0.75",
    )
    view = ledger.view()
    assert view.cash_gbp == Decimal("467.60")
    assert view.positions[0].total_cost_gbp == Decimal("32.00")
    assert view.positions[0].market_value_gbp == Decimal("30.00")
    assert view.positions[0].unrealised_pnl_gbp == Decimal("-2.00")
    assert view.equity_gbp == Decimal("497.60")
    metrics = ledger.daily_metrics(june(2, 11, 2), rules)
    assert metrics.day_opening_equity_gbp == Decimal("500.00")
    assert metrics.day_loss_gbp == Decimal("2.40")
    assert metrics.daily_loss_threshold_gbp == Decimal("5.00")


def test_fractional_quantity() -> None:
    # Buy 0.5 @ 10 = 5.00. Sell 0.25 @ 12 = 3.00 proceeds.
    # Cost relieved = round(5.00 * 0.25 / 0.5) = 2.50. Realised 0.50.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-frac",
        timestamp=june(3, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="0.5",
        price="10.00",
    )
    settle(
        ledger,
        event_id="sell-frac",
        timestamp=june(3, 11),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="0.25",
        price="12.00",
    )
    view = ledger.view()
    assert view.cash_gbp == Decimal("498.00")
    assert view.realised_pnl_gbp == Decimal("0.50")
    assert view.positions[0].quantity == Decimal("0.25")
    assert view.positions[0].total_cost_gbp == Decimal("2.50")


def test_three_ten_pence_fees_sum_exactly() -> None:
    ledger = Ledger("1.00")
    for index in range(3):
        fee(
            ledger,
            event_id=f"fee-{index}",
            timestamp=june(4, 10, index),
            fee_type="commission",
            amount="0.10",
        )
    assert ledger.view().cash_gbp == Decimal("0.70")
    assert ledger.view().equity_gbp == Decimal("0.70")


def test_unmarked_position_is_carried_at_cost() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-cost",
        timestamp=june(4, 12),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    view = ledger.view()
    assert view.positions[0].mark_price is None
    assert view.positions[0].market_value_gbp == Decimal("20.00")
    assert view.positions[0].unrealised_pnl_gbp == Decimal("0.00")
    assert view.equity_gbp == Decimal("500.00")


def test_pending_fill_does_not_change_equity() -> None:
    from sterling.ledger.events import FillEvent

    ledger = Ledger("500.00")
    ledger.post(FillEvent("buy-pending", june(4, 13), "SYNTH-EQ", "buy", "2", "10.00", "GBP"))
    view = ledger.view()
    assert view.pending_fill_ids == ("buy-pending",)
    assert view.positions == ()
    assert view.cash_gbp == Decimal("500.00")
    assert view.equity_gbp == Decimal("500.00")

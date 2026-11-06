"""Late entry fees stay on the fill that incurred them. Prices are synthetic."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sterling.errors import InvalidEvent
from sterling.ledger.ledger import Ledger
from tests.support import deposit, fee, june, settle


def test_late_fee_after_partial_close_splits_across_that_fill_only() -> None:
    # Buy 10 @ 10. Sell 4 @ 12. Price P&L = 48 - 40 = 8.00 before the fee.
    # Late commission 1.00 on that buy: closed 4/10 → 0.40 on the close, 0.60 still open.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="10",
        price="10.00",
    )
    settle(
        ledger,
        event_id="sell-a",
        timestamp=june(1, 11),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="4",
        price="12.00",
    )
    assert ledger.closed_trades()[0].net_gbp == Decimal("8.00")
    fee(ledger, event_id="fee-late", timestamp=june(1, 12), fee_type="commission", amount="1.00", related_event_id="buy-a")
    trade = ledger.closed_trades()[0]
    assert trade.allocated_entry_fees_gbp == Decimal("0.40")
    assert trade.net_gbp == Decimal("7.60")
    assert ledger.view().positions[0].quantity == Decimal("6")
    assert ledger.view().positions[0].unallocated_entry_fees_gbp == Decimal("0.60")
    assert ledger.view().equity_gbp == Decimal("507.00")


def test_late_fee_after_full_close_does_not_touch_a_reentry() -> None:
    # Close A at a 1.00 price gain, re-enter B flat to cost, then bill A 0.50.
    # A's net becomes 0.50. B's later close stays 0.00.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(2, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="10.00",
    )
    settle(
        ledger,
        event_id="sell-a",
        timestamp=june(2, 11),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="1",
        price="11.00",
    )
    settle(
        ledger,
        event_id="buy-b",
        timestamp=june(2, 12),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="20.00",
    )
    fee(ledger, event_id="fee-a", timestamp=june(2, 13), fee_type="commission", amount="0.50", related_event_id="buy-a")
    assert ledger.view().positions[0].unallocated_entry_fees_gbp == Decimal("0.00")
    settle(
        ledger,
        event_id="sell-b",
        timestamp=june(2, 14),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="1",
        price="20.00",
    )
    first, second = ledger.closed_trades()
    assert first.fill_id == "sell-a"
    assert first.net_gbp == Decimal("0.50")
    assert first.allocated_entry_fees_gbp == Decimal("0.50")
    assert second.fill_id == "sell-b"
    assert second.allocated_entry_fees_gbp == Decimal("0.00")
    assert second.net_gbp == Decimal("0.00")
    assert ledger.view().equity_gbp == Decimal("500.50")


def test_late_fee_after_several_buys_stays_on_the_named_fill() -> None:
    # A 2 @ 10 and B 2 @ 30. Sell 2 @ 20. Average-cost relief is 40, price P&L 0.
    # Each lot contributes 1 share. Late 1.00 on A: half closed → 0.50 on the close, 0.50 still on A.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(3, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    settle(
        ledger,
        event_id="buy-b",
        timestamp=june(3, 11),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="30.00",
    )
    settle(
        ledger,
        event_id="sell-1",
        timestamp=june(3, 12),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="2",
        price="20.00",
    )
    fee(ledger, event_id="fee-a", timestamp=june(3, 13), fee_type="stamp_duty", amount="1.00", related_event_id="buy-a")
    trade = ledger.closed_trades()[0]
    assert trade.price_pnl_gbp == Decimal("0.00")
    assert trade.allocated_entry_fees_gbp == Decimal("0.50")
    assert trade.net_gbp == Decimal("-0.50")
    assert ledger.view().positions[0].quantity == Decimal("2")
    assert ledger.view().positions[0].unallocated_entry_fees_gbp == Decimal("0.50")
    assert ledger.consecutive_net_closed_losses() == 1


def test_late_fee_on_the_fx_event_of_an_old_fill() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-usd",
        timestamp=june(4, 10),
        instrument_id="SYNTH-US",
        side="buy",
        quantity="1",
        price="10",
        currency="USD",
        fx_rate="0.80",
    )
    settle(
        ledger,
        event_id="sell-usd",
        timestamp=june(4, 11),
        instrument_id="SYNTH-US",
        side="sell",
        quantity="1",
        price="10",
        currency="USD",
        fx_rate="0.80",
    )
    fee(
        ledger,
        event_id="fee-fx",
        timestamp=june(4, 12),
        fee_type="fx_fee",
        amount="0.40",
        related_event_id="fx-buy-usd",
    )
    trade = ledger.closed_trades()[0]
    assert trade.price_pnl_gbp == Decimal("0.00")
    assert trade.allocated_entry_fees_gbp == Decimal("0.40")
    assert trade.net_gbp == Decimal("-0.40")
    assert ledger.view().equity_gbp == Decimal("499.60")
    assert ledger.view().positions == ()


def test_late_fee_replay_matches_after_restart(tmp_path: Path) -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(5, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="10",
        price="10.00",
    )
    settle(
        ledger,
        event_id="sell-a",
        timestamp=june(5, 11),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="4",
        price="12.00",
    )
    fee(ledger, event_id="fee-late", timestamp=june(5, 12), fee_type="commission", amount="1.00", related_event_id="buy-a")
    path = tmp_path / "late.jsonl"
    ledger.save(path)
    loaded = Ledger.load(path)
    assert loaded.closed_trades() == ledger.closed_trades()
    assert loaded.view() == ledger.view()
    assert loaded.consecutive_net_closed_losses() == ledger.consecutive_net_closed_losses()


def test_unknown_and_unlinked_related_ids_are_rejected() -> None:
    ledger = Ledger("500.00")
    deposit(ledger, "dep-1", june(6, 10), "10.00")
    with pytest.raises(InvalidEvent, match="does not match"):
        fee(ledger, event_id="fee-missing", timestamp=june(6, 11), fee_type="commission", amount="0.10", related_event_id="missing")
    with pytest.raises(InvalidEvent, match="fill or its FX"):
        fee(ledger, event_id="fee-dep", timestamp=june(6, 11), fee_type="commission", amount="0.10", related_event_id="dep-1")
    assert ledger.view().cash_gbp == Decimal("510.00")
    assert ledger.view().fees_gbp[0][1] == Decimal("0.00")

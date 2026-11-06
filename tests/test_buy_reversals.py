"""Buy reversals that would rewrite a later sale are refused. Figures are synthetic."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sterling.errors import ReversalRejected
from sterling.ledger.ledger import Ledger
from tests.support import fee, june, reversal, settle


def test_reversing_an_earlier_buy_under_a_later_sell_is_refused() -> None:
    # A 2 @ 10, B 3 @ 12, sell 1 @ 12.
    # Cost relief = round(56 * 1 / 5) = 11.20. Price P&L = 0.80. Qty left 4, cost 44.80.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="2",
        price="10.00",
    )
    settle(
        ledger,
        event_id="buy-b",
        timestamp=june(1, 11),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="3",
        price="12.00",
    )
    settle(
        ledger,
        event_id="sell-1",
        timestamp=june(1, 12),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="1",
        price="12.00",
    )
    before = ledger.view()
    with pytest.raises(ReversalRejected, match="later sell"):
        reversal(ledger, "rev-a", june(1, 13), "fx-buy-a", "void the earlier buy")
    assert ledger.view() == before
    assert ledger.view().positions[0].quantity == Decimal("4")
    assert ledger.view().positions[0].total_cost_gbp == Decimal("44.80")
    assert ledger.view().realised_pnl_gbp == Decimal("0.80")
    assert ledger.closed_trades()[0].net_gbp == Decimal("0.80")


def test_partial_exit_reversal_restores_quantity_cost_and_entry_fees() -> None:
    # Buy 10 @ 10 with a 1.00 entry fee. Sell 4 @ 12.
    # Price P&L 8.00. Entry fee relieved round(1.00 * 4 / 10) = 0.40. Net 7.60.
    # Reversing the sell restores qty 10, cost 100, and the full 1.00 fee on the open lot.
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(2, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="10",
        price="10.00",
    )
    fee(ledger, event_id="fee-a", timestamp=june(2, 10, 1), fee_type="commission", amount="1.00", related_event_id="buy-a")
    settle(
        ledger,
        event_id="sell-1",
        timestamp=june(2, 11),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="4",
        price="12.00",
    )
    assert ledger.closed_trades()[0].net_gbp == Decimal("7.60")
    reversal(ledger, "rev-sell", june(2, 12), "fx-sell-1", "undo the partial sale")
    view = ledger.view()
    assert view.positions[0].quantity == Decimal("10")
    assert view.positions[0].total_cost_gbp == Decimal("100.00")
    assert view.positions[0].unallocated_entry_fees_gbp == Decimal("1.00")
    assert view.realised_pnl_gbp == Decimal("0.00")
    assert view.cash_gbp == Decimal("399.00")
    assert ledger.closed_trades() == ()
    assert ledger.consecutive_net_closed_losses() == 0


def test_buy_can_be_reversed_after_the_dependent_sell_is_reversed() -> None:
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
        quantity="3",
        price="12.00",
    )
    settle(
        ledger,
        event_id="sell-1",
        timestamp=june(3, 12),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="1",
        price="12.00",
    )
    reversal(ledger, "rev-sell", june(3, 13), "fx-sell-1", "undo sale first")
    reversal(ledger, "rev-a", june(3, 14), "fx-buy-a", "void buy A")
    view = ledger.view()
    assert view.positions[0].quantity == Decimal("3")
    assert view.positions[0].total_cost_gbp == Decimal("36.00")
    assert view.realised_pnl_gbp == Decimal("0.00")
    assert view.cash_gbp == Decimal("464.00")


def test_reversal_state_matches_after_restart(tmp_path: Path) -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-a",
        timestamp=june(4, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="10",
        price="10.00",
    )
    fee(ledger, event_id="fee-a", timestamp=june(4, 10, 1), fee_type="commission", amount="1.00", related_event_id="buy-a")
    settle(
        ledger,
        event_id="sell-1",
        timestamp=june(4, 11),
        instrument_id="SYNTH-EQ",
        side="sell",
        quantity="4",
        price="12.00",
    )
    reversal(ledger, "rev-sell", june(4, 12), "fx-sell-1", "undo the partial sale")
    path = tmp_path / "reversal.jsonl"
    ledger.save(path)
    loaded = Ledger.load(path)
    assert loaded.view() == ledger.view()
    assert loaded.closed_trades() == ledger.closed_trades()
    assert loaded.events == ledger.events

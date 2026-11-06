"""Pending fills reserve cash or quantity. Synthetic instruments only."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sterling.errors import InvalidEvent, NegativeCashRejected, OversellRejected
from sterling.ledger.events import FillEvent, FxEvent
from sterling.ledger.ledger import Ledger
from tests.support import june, settle, withdraw


def test_two_pending_buys_cannot_over_reserve_and_block_the_withdrawal() -> None:
    ledger = Ledger("500.00")
    ledger.post(FillEvent("buy-1", june(1, 10), "SYNTH-EQ", "buy", "400", "1.00", "GBP"))
    view = ledger.view()
    assert view.available_cash_gbp == Decimal("100.00")
    assert view.available_cash_status == "KNOWN"
    assert view.gross_exposure_gbp == Decimal("400.00")
    assert view.exposure_status == "KNOWN"
    with pytest.raises(NegativeCashRejected):
        ledger.post(FillEvent("buy-2", june(1, 11), "SYNTH-EQ", "buy", "400", "1.00", "GBP"))
    with pytest.raises(NegativeCashRejected):
        withdraw(ledger, "wd-1", june(1, 12), "200.00")
    withdraw(ledger, "wd-2", june(1, 13), "100.00")
    assert ledger.view().cash_gbp == Decimal("400.00")
    assert ledger.view().available_cash_gbp == Decimal("0.00")
    assert ledger.view().pending_fill_ids == ("buy-1",)


def test_rejected_pending_sell_cannot_close_a_later_reentry() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="buy-1",
        timestamp=june(2, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="10.00",
    )
    ledger.post(FillEvent("sell-1", june(2, 11), "SYNTH-EQ", "sell", "1", "10.00", "GBP"))
    with pytest.raises(InvalidEvent, match="fx_rate_to_gbp of 1"):
        ledger.post(FxEvent("fx-bad", june(2, 12), "sell-1", "0.99"))
    assert "sell-1" not in ledger.view().pending_fill_ids
    assert "void-sell-1" in ledger.view().event_ids
    settle(
        ledger,
        event_id="buy-2",
        timestamp=june(2, 13),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="10.00",
    )
    with pytest.raises(InvalidEvent, match="not pending"):
        ledger.post(FxEvent("fx-later", june(2, 14), "sell-1", "1"))
    assert ledger.view().positions[0].quantity == Decimal("2")
    assert ledger.closed_trades() == ()


def test_short_pending_sell_is_rejected_at_fill_time() -> None:
    ledger = Ledger("500.00")
    with pytest.raises(OversellRejected):
        ledger.post(FillEvent("sell-short", june(3, 10), "SYNTH-EQ", "sell", "1", "10.00", "GBP"))
    assert ledger.events == ()
    assert ledger.view().pending_fill_ids == ()


def test_unknown_fx_pending_buy_reports_unknown() -> None:
    ledger = Ledger("500.00")
    ledger.post(FillEvent("buy-usd", june(4, 10), "SYNTH-US", "buy", "1", "10", "USD"))
    view = ledger.view()
    assert view.pending_fill_ids == ("buy-usd",)
    assert view.available_cash_status == "UNKNOWN"
    assert view.available_cash_gbp is None
    assert view.exposure_status == "UNKNOWN"
    assert view.gross_exposure_gbp is None
    with pytest.raises(InvalidEvent, match="UNKNOWN"):
        withdraw(ledger, "wd-1", june(4, 11), "1.00")
    assert ledger.view().cash_gbp == Decimal("500.00")

"""Explicit half-even rounding to pence."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sterling.errors import InvalidEvent, MoneyError
from sterling.ledger.ledger import Ledger
from sterling.ledger.money import gbp_notional, quantize_money
from tests.support import june, settle

_CASES = [
    ("0.015", "0.02"),
    ("0.025", "0.02"),
    ("1.005", "1.00"),
    ("2.495", "2.50"),
    ("2.485", "2.48"),
    ("11.867775", "11.87"),
    ("0.812", "0.81"),
    ("-0.015", "-0.02"),
    ("0", "0.00"),
]


@pytest.mark.parametrize(("raw", "expected"), _CASES)
def test_half_even_pence(raw: str, expected: str) -> None:
    assert quantize_money(Decimal(raw)) == Decimal(expected)
    assert quantize_money(quantize_money(Decimal(raw))) == Decimal(expected)


def test_notional_rounds_once() -> None:
    assert gbp_notional(Decimal("1.5"), Decimal("10.015"), Decimal("0.79")) == Decimal("11.87")
    assert quantize_money(Decimal("1.015") * Decimal("0.80")) == Decimal("0.81")


def test_ledger_posts_the_rounded_notional() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="fill-round",
        timestamp=june(1, 10),
        instrument_id="SYNTH-US",
        side="buy",
        quantity="1.5",
        price="10.015",
        currency="USD",
        fx_rate="0.79",
    )
    view = ledger.view()
    assert view.cash_gbp == Decimal("488.13")
    assert view.positions[0].total_cost_gbp == Decimal("11.87")


def test_sub_penny_gbp_price_rounds_on_settlement() -> None:
    ledger = Ledger("500.00")
    settle(
        ledger,
        event_id="fill-penny",
        timestamp=june(1, 10),
        instrument_id="SYNTH-EQ",
        side="buy",
        quantity="1",
        price="0.015",
    )
    assert ledger.view().cash_gbp == Decimal("499.98")


def test_zero_rounded_notional_is_rejected() -> None:
    ledger = Ledger("500.00")
    settle_kwargs = {
        "event_id": "fill-dust",
        "timestamp": june(1, 10),
        "instrument_id": "SYNTH-EQ",
        "side": "buy",
        "quantity": "0.00000001",
        "price": "0.00000001",
    }
    from sterling.ledger.events import FillEvent

    with pytest.raises(InvalidEvent, match="zero"):
        ledger.post(FillEvent("fill-dust", june(1, 10), "SYNTH-EQ", "buy", "0.00000001", "0.00000001", "GBP"))
    assert ledger.view().cash_gbp == Decimal("500.00")
    assert ledger.view().positions == ()
    assert settle_kwargs["instrument_id"] == "SYNTH-EQ"


def test_float_money_is_rejected() -> None:
    from sterling.ledger.events import DepositEvent

    with pytest.raises(MoneyError):
        DepositEvent("dep-float", june(1, 10), 1.25)  # type: ignore[arg-type]

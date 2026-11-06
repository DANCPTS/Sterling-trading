"""GBP reservation implied by a pending buy. Nothing here approves an order.

A GBP buy reserves ``round(quantity * price) + fee_allowance_gbp``.
A non-GBP buy reserves ``round(quantity * price * fx_rate_ceiling) +
fee_allowance_gbp`` when the caller set a ceiling. The allowance is the
caller's provision for commission, FX fee, and stamp duty/SDRT still to be
posted. Settlement may not use an FX rate above that ceiling.

``None`` means the GBP amount cannot be determined (FX unknown). Callers must
treat that as UNKNOWN and Stage 2 must fail closed on UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from sterling.ledger.events import FillEvent
from sterling.ledger.money import gbp_notional, quantize_money

AvailabilityStatus = Literal["KNOWN", "UNKNOWN"]


@dataclass(frozen=True, slots=True)
class Availability:
    """Accounting availability. ``UNKNOWN`` is not a licence to withdraw or trade."""

    available_cash_gbp: Decimal | None
    available_cash_status: AvailabilityStatus
    gross_exposure_gbp: Decimal | None
    exposure_status: AvailabilityStatus


def summarise_availability(
    *,
    cash_gbp: Decimal,
    pending_buys: tuple[FillEvent, ...] | list[FillEvent],
    settled_market_value_gbp: Decimal,
) -> Availability:
    reserved = Decimal("0.00")
    unknown = False
    for fill in pending_buys:
        amount = buy_reservation_gbp(fill)
        if amount is None:
            unknown = True
        else:
            reserved = quantize_money(reserved + amount)
    if unknown:
        return Availability(None, "UNKNOWN", None, "UNKNOWN")
    return Availability(
        quantize_money(cash_gbp - reserved),
        "KNOWN",
        quantize_money(settled_market_value_gbp + reserved),
        "KNOWN",
    )


def buy_reservation_gbp(fill: FillEvent) -> Decimal | None:
    """Cash to hold back for a pending buy, or None when the rate is unknown."""
    allowance = fill.fee_allowance_gbp
    if fill.instrument_currency == "GBP":
        consideration = gbp_notional(fill.quantity, fill.price, Decimal("1"))
    elif fill.fx_rate_ceiling is None:
        return None
    else:
        consideration = gbp_notional(fill.quantity, fill.price, fill.fx_rate_ceiling)
    return quantize_money(consideration + allowance)

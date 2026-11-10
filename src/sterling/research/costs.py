"""Simulated dealing costs.

``research_cost_model`` is the long-history schedule. The numbers were chosen
before that sample was run: 10 bps spread and 5 bps slippage from 2010, 25 bps
spread and 10 bps slippage before 2010, and an extra 15 bps of slippage inside
fixed stress windows (Mar 2000–Mar 2003, Oct 2007–Mar 2009, 20 Feb–23 Mar 2020,
and calendar 2022). Commission is zero. Stamp stays available at 0.5% and is
applied only when the instrument is not exempt. Spread and slippage move the
fill off the open and are not charged again. Stamp is 0.5% on buys of
non-exempt shares and zero on sells. ETF and ETC lines are exempt here; that
is not tax advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_UP, Decimal

from sterling.research.errors import ResearchError
from sterling.research.money import QTY_QUANTUM, ZERO_MONEY, quantize_money, quantize_qty_down


@dataclass(frozen=True, slots=True)
class CostModel:
    commission_bps: Decimal = Decimal("0")
    commission_flat_gbp: Decimal = Decimal("0")
    spread_bps: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("0")
    fx_conversion_bps: Decimal = Decimal("0")
    stamp_duty_rate: Decimal = Decimal("0.005")


# Fixed before the long-history run. Not chosen from those results.
_EARLY_COST_END = date(2010, 1, 1)
_CRISIS_WINDOWS = (
    (date(2000, 3, 1), date(2003, 3, 31)),
    (date(2007, 10, 1), date(2009, 3, 31)),
    (date(2020, 2, 20), date(2020, 3, 23)),
    (date(2022, 1, 1), date(2022, 12, 31)),
)


def research_cost_model(day: date) -> CostModel:
    """Date-dependent research cost assumption. Not a measured spread."""
    early = day < _EARLY_COST_END
    crisis = any(start <= day <= end for start, end in _CRISIS_WINDOWS)
    spread = Decimal("25") if early else Decimal("10")
    slippage = Decimal("10") if early else Decimal("5")
    if crisis:
        slippage += Decimal("15")
    return CostModel(
        commission_flat_gbp=Decimal("0"),
        commission_bps=Decimal("0"),
        spread_bps=spread,
        slippage_bps=slippage,
        fx_conversion_bps=Decimal("0"),
        stamp_duty_rate=Decimal("0.005"),
    )


@dataclass(frozen=True, slots=True)
class TradeQuote:
    side: str
    quantity: Decimal
    mid_price_gbp: Decimal
    exec_price_gbp: Decimal
    notional_gbp: Decimal
    mid_notional_gbp: Decimal
    commission_gbp: Decimal
    spread_cost_gbp: Decimal
    slippage_cost_gbp: Decimal
    stamp_duty_gbp: Decimal
    fx_cost_gbp: Decimal

    @property
    def explicit_cost_gbp(self) -> Decimal:
        return self.commission_gbp + self.stamp_duty_gbp + self.fx_cost_gbp

    @property
    def total_cost_gbp(self) -> Decimal:
        return self.explicit_cost_gbp + self.spread_cost_gbp + self.slippage_cost_gbp

    @property
    def cash_delta_gbp(self) -> Decimal:
        if self.side == "BUY":
            return -(self.notional_gbp + self.explicit_cost_gbp)
        if self.side == "SELL":
            return self.notional_gbp - self.explicit_cost_gbp
        raise ResearchError(f"unknown side {self.side}")


def execution_price(mid: Decimal, side: str, model: CostModel) -> Decimal:
    bump = (model.spread_bps / Decimal(2) + model.slippage_bps) / Decimal(10000)
    if side == "BUY":
        price = mid * (Decimal(1) + bump)
    elif side == "SELL":
        price = mid * (Decimal(1) - bump)
    else:
        raise ResearchError(f"unknown side {side}")
    if price <= 0:
        raise ResearchError("execution price is not positive")
    return price


def quote_trade(
    side: str,
    quantity: Decimal,
    mid: Decimal,
    model: CostModel,
    *,
    stamp: bool,
    charge_fx: bool,
) -> TradeQuote:
    if quantity < 0 or mid <= 0:
        raise ResearchError("trade quantity and mid price must be positive")
    if quantity == 0:
        return TradeQuote(
            side, quantity, mid, mid, ZERO_MONEY, ZERO_MONEY, ZERO_MONEY, ZERO_MONEY, ZERO_MONEY, ZERO_MONEY, ZERO_MONEY
        )
    price = execution_price(mid, side, model)
    notional = quantize_money(quantity * price)
    mid_notional = quantize_money(quantity * mid)
    gap = abs(notional - mid_notional)
    half_spread = model.spread_bps / Decimal(2)
    total_bps = half_spread + model.slippage_bps
    if gap > 0 and total_bps > 0:
        spread_cost = quantize_money(gap * half_spread / total_bps)
        if spread_cost > gap:
            spread_cost = gap
        slippage_cost = gap - spread_cost
    else:
        spread_cost = ZERO_MONEY
        slippage_cost = gap if gap != 0 else ZERO_MONEY
    commission = quantize_money(model.commission_flat_gbp + notional * model.commission_bps / Decimal(10000))
    stamp_amount = quantize_money(notional * model.stamp_duty_rate) if stamp and side == "BUY" else ZERO_MONEY
    fx_amount = quantize_money(notional * model.fx_conversion_bps / Decimal(10000)) if charge_fx else ZERO_MONEY
    return TradeQuote(
        side=side,
        quantity=quantity,
        mid_price_gbp=mid,
        exec_price_gbp=price,
        notional_gbp=notional,
        mid_notional_gbp=mid_notional,
        commission_gbp=commission,
        spread_cost_gbp=spread_cost,
        slippage_cost_gbp=slippage_cost,
        stamp_duty_gbp=stamp_amount,
        fx_cost_gbp=fx_amount,
    )


def affordable_buy(
    spend_gbp: Decimal,
    mid: Decimal,
    model: CostModel,
    *,
    stamp: bool,
    charge_fx: bool,
) -> TradeQuote:
    """Largest buy whose cash debit, including extra costs, does not exceed ``spend_gbp``."""
    if spend_gbp <= 0:
        return quote_trade("BUY", Decimal(0), mid, model, stamp=stamp, charge_fx=charge_fx)
    price = execution_price(mid, "BUY", model)
    variable = model.commission_bps / Decimal(10000)
    if stamp:
        variable += model.stamp_duty_rate
    if charge_fx:
        variable += model.fx_conversion_bps / Decimal(10000)
    unit = price * (Decimal(1) + variable)
    flat = model.commission_flat_gbp
    if spend_gbp <= flat or unit <= 0:
        return quote_trade("BUY", Decimal(0), mid, model, stamp=stamp, charge_fx=charge_fx)
    quantity = quantize_qty_down((spend_gbp - flat) / unit)
    quote = quote_trade("BUY", quantity, mid, model, stamp=stamp, charge_fx=charge_fx)
    for _ in range(8):
        debit = -quote.cash_delta_gbp
        if debit <= spend_gbp or quantity == 0:
            break
        excess = debit - spend_gbp
        step = (excess / price).quantize(QTY_QUANTUM, rounding=ROUND_UP)
        if step < QTY_QUANTUM:
            step = QTY_QUANTUM
        quantity = max(Decimal(0), quantity - step)
        quote = quote_trade("BUY", quantity, mid, model, stamp=stamp, charge_fx=charge_fx)
    if -quote.cash_delta_gbp > spend_gbp:
        return quote_trade("BUY", Decimal(0), mid, model, stamp=stamp, charge_fx=charge_fx)
    return quote

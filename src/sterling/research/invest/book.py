"""Paper portfolio maths for Sterling Invest. No leverage, no short, no borrow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sterling.research.costs import TradeQuote, affordable_buy, quote_trade, research_cost_model
from sterling.research.errors import ResearchError
from sterling.research.invest.registration import NO_TRADE_BAND, STARTING_CASH_GBP
from sterling.research.invest.schema import CASH, FUNDS
from sterling.research.money import ZERO, ZERO_MONEY, quantize_money, quantize_qty_down

# Ongoing charges are already inside the exchange price. Deducting them again would
# double-count. Spread, slippage and stamp still apply to simulated fills.
OCF_NOTE = "Exchange prices are already net of fund OCF. This book deducts no further OCF."


@dataclass
class Receivable:
    ticker: str
    amount: Decimal
    pay_on: date


@dataclass
class Book:
    cash: Decimal = STARTING_CASH_GBP
    quantities: dict[str, Decimal] = field(default_factory=dict)
    receivables: list[Receivable] = field(default_factory=list)
    cumulative_costs: Decimal = ZERO_MONEY
    interest: Decimal = ZERO_MONEY
    trades: int = 0
    pending: dict[str, Decimal] | None = None
    decided_at: datetime | None = None
    last_price: dict[str, tuple[Decimal, date]] = field(default_factory=dict)
    nav_rows: list[tuple[date, Decimal]] = field(default_factory=list)
    peak: Decimal = STARTING_CASH_GBP
    max_drawdown: Decimal = ZERO
    accrued_through: date | None = None

    def quantity(self, ticker: str) -> Decimal:
        return self.quantities.get(ticker, ZERO)


def declare_distribution(book: Book, ticker: str, amount: Decimal, pay_on: date) -> None:
    if amount < 0:
        raise ResearchError("distribution cannot be negative")
    if amount == 0:
        return
    book.receivables.append(Receivable(ticker=ticker, amount=amount, pay_on=pay_on))


def pay_distributions(book: Book, session: date) -> Decimal:
    due = [item for item in book.receivables if item.pay_on <= session]
    book.receivables = [item for item in book.receivables if item.pay_on > session]
    paid = quantize_money(sum((item.amount for item in due), ZERO))
    book.cash = quantize_money(book.cash + paid)
    _guard_cash(book)
    return paid


def accrue_cash(book: Book, annual_rate: Decimal) -> Decimal:
    """One calendar day of retail deposit interest. A negative rate earns nothing."""
    _guard_cash(book)
    rate = annual_rate if annual_rate > 0 else ZERO
    interest = quantize_money(book.cash * rate / Decimal(365))
    book.cash = quantize_money(book.cash + interest)
    book.interest = quantize_money(book.interest + interest)
    return interest


def mark(book: Book, prices: dict[str, Decimal], session: date) -> tuple[Decimal, dict[str, str]] | None:
    """Value every holding with its own price. A missing holding price is invalid."""
    labels: dict[str, str] = {}
    total = book.cash
    for ticker in FUNDS:
        quantity = book.quantity(ticker)
        if quantity == 0:
            continue
        price = prices.get(ticker)
        label = "close"
        if price is None:
            remembered = book.last_price.get(ticker)
            if remembered is None or (session - remembered[1]).days > 7:
                return None
            price = remembered[0]
            label = "stale_close"
        if price <= 0:
            return None
        total += quantity * price
        labels[ticker] = label
        book.last_price[ticker] = (price, session)
    for ticker, price in prices.items():
        if price > 0:
            book.last_price[ticker] = (price, session)
    nav = quantize_money(total + sum((item.amount for item in book.receivables), ZERO))
    if nav <= 0:
        return None
    book.nav_rows.append((session, nav))
    if nav > book.peak:
        book.peak = nav
    drawdown = (book.peak - nav) / book.peak if book.peak > 0 else ZERO
    if drawdown > book.max_drawdown:
        book.max_drawdown = drawdown
    return nav, labels


def _probe_nav(book: Book, prices: dict[str, Decimal], session: date) -> tuple[Decimal, dict[str, str]] | None:
    """Value the book without keeping the mark on the high-water path.

    Sizing and a decision snapshot call this. The peak and the drawdown stay
    where the last committed daily close left them, including when the probe
    raises.
    """
    peak, drawdown = book.peak, book.max_drawdown
    before = len(book.nav_rows)
    try:
        return mark(book, prices, session)
    finally:
        if len(book.nav_rows) > before:
            del book.nav_rows[before:]
        book.peak = peak
        book.max_drawdown = drawdown


def weights_of(book: Book, prices: dict[str, Decimal], session: date) -> dict[str, Decimal] | None:
    marked = _probe_nav(book, prices, session)
    if marked is None:
        return None
    nav, _labels = marked
    weights = {CASH: book.cash / nav}
    for ticker in FUNDS:
        quantity = book.quantity(ticker)
        price = prices.get(ticker) or (book.last_price.get(ticker) or (ZERO, session))[0]
        weights[ticker] = (quantity * price / nav) if quantity > 0 and price > 0 else ZERO
    return weights


def apply_targets(
    book: Book,
    targets: dict[str, Decimal],
    prices: dict[str, Decimal],
    tradable: set[str],
    session: date,
) -> list[dict[str, str]]:
    """Sell first, then buy. A name that cannot be priced is left unchanged."""
    current = weights_of(book, prices, session)
    if current is None:
        return []
    valued = _probe_nav(book, prices, session)
    if valued is None:
        return []
    nav = valued[0]
    adjusted = _lock_unpriced(targets, current, tradable)
    model = research_cost_model(session)
    fills: list[dict[str, str]] = []
    for ticker in FUNDS:
        if ticker not in tradable or ticker not in prices:
            continue
        if abs(adjusted[ticker] - current[ticker]) < NO_TRADE_BAND:
            continue
        price = prices[ticker]
        held = book.quantity(ticker)
        target_qty = quantize_qty_down(adjusted[ticker] * nav / price)
        if target_qty < held:
            fills.extend(_sell(book, ticker, held - target_qty, price, session, model))
    for ticker in FUNDS:
        if ticker not in tradable or ticker not in prices:
            continue
        if abs(adjusted[ticker] - current[ticker]) < NO_TRADE_BAND:
            continue
        price = prices[ticker]
        held = book.quantity(ticker)
        target_qty = quantize_qty_down(adjusted[ticker] * nav / price)
        if target_qty > held:
            fills.extend(_buy(book, ticker, target_qty - held, price, session, model))
    _guard_cash(book)
    for ticker, quantity in book.quantities.items():
        if quantity < 0:
            raise ResearchError(f"{ticker} quantity is short")
    gross = sum((book.quantity(ticker) * prices.get(ticker, ZERO) for ticker in FUNDS), ZERO)
    if gross > nav + Decimal("0.05"):
        raise ResearchError("gross exposure is above the portfolio")
    return fills


def trade_toward(
    book: Book,
    ticker: str,
    target_weight: Decimal,
    open_price: Decimal,
    marks: dict[str, Decimal],
    session: date,
) -> list[dict[str, str]]:
    """Trade one fund toward its target. Other names are not rescaled."""
    current = weights_of(book, marks, session)
    if current is None:
        return []
    if abs(target_weight - current.get(ticker, ZERO)) < NO_TRADE_BAND:
        return []
    nav = book.cash + sum((book.quantity(name) * marks.get(name, ZERO) for name in FUNDS), ZERO)
    if nav <= 0 or open_price <= 0:
        return []
    target_qty = quantize_qty_down(target_weight * nav / open_price)
    held = book.quantity(ticker)
    model = research_cost_model(session)
    if target_qty < held:
        return _sell(book, ticker, held - target_qty, open_price, session, model)
    return _buy(book, ticker, target_qty - held, open_price, session, model)


def settle_pending(book: Book, prices: dict[str, Decimal], tradable: set[str], session: date) -> list[dict[str, str]]:
    if not book.pending:
        return []
    fills = apply_targets(book, book.pending, prices, tradable, session)
    book.pending = None
    return fills


def derived_actions(current: dict[str, Decimal], targets: dict[str, Decimal], tradable: set[str]) -> dict[str, str]:
    actions: dict[str, str] = {}
    for ticker in FUNDS:
        if ticker not in tradable:
            actions[ticker] = "HOLD"
            continue
        gap = targets[ticker] - current.get(ticker, ZERO)
        if gap > NO_TRADE_BAND:
            actions[ticker] = "BUY"
        elif gap < -NO_TRADE_BAND:
            actions[ticker] = "SELL"
        else:
            actions[ticker] = "HOLD"
    return actions


def _lock_unpriced(targets: dict[str, Decimal], current: dict[str, Decimal], tradable: set[str]) -> dict[str, Decimal]:
    locked = [ticker for ticker in FUNDS if ticker not in tradable]
    locked_weight = sum((current.get(ticker, ZERO) for ticker in locked), ZERO)
    residual = Decimal(1) - locked_weight
    if residual < 0:
        residual = ZERO
    free = [ticker for ticker in FUNDS if ticker in tradable]
    free_target = sum((targets[ticker] for ticker in free), ZERO) + targets[CASH]
    adjusted = {ticker: current.get(ticker, ZERO) for ticker in locked}
    if free_target <= 0:
        adjusted[CASH] = residual
        for ticker in free:
            adjusted[ticker] = ZERO
        return adjusted
    scale = residual / free_target
    for ticker in free:
        adjusted[ticker] = targets[ticker] * scale
    adjusted[CASH] = residual - sum((adjusted[ticker] for ticker in free), ZERO)
    return adjusted


def _sell(book: Book, ticker: str, quantity: Decimal, price: Decimal, session: date, model: object) -> list[dict[str, str]]:
    quantity = quantize_qty_down(min(quantity, book.quantity(ticker)))
    if quantity <= 0:
        return []
    quote = quote_trade("SELL", quantity, price, model, stamp=False, charge_fx=False)  # type: ignore[arg-type]
    book.quantities[ticker] = quantize_qty_down(book.quantity(ticker) - quantity)
    book.cash = quantize_money(book.cash + quote.cash_delta_gbp)
    book.cumulative_costs = quantize_money(book.cumulative_costs + quote.total_cost_gbp)
    book.trades += 1
    return [_fill_row(ticker, quote, session)]


def _buy(book: Book, ticker: str, quantity: Decimal, price: Decimal, session: date, model: object) -> list[dict[str, str]]:
    if quantity <= 0 or book.cash <= 0:
        return []
    wanted = quote_trade("BUY", quantity, price, model, stamp=False, charge_fx=False)  # type: ignore[arg-type]
    spend = min(book.cash, -wanted.cash_delta_gbp)
    quote = affordable_buy(spend, price, model, stamp=False, charge_fx=False)  # type: ignore[arg-type]
    if quote.quantity <= 0:
        return []
    book.quantities[ticker] = quantize_qty_down(book.quantity(ticker) + quote.quantity)
    book.cash = quantize_money(book.cash + quote.cash_delta_gbp)
    book.cumulative_costs = quantize_money(book.cumulative_costs + quote.total_cost_gbp)
    book.trades += 1
    return [_fill_row(ticker, quote, session)]


def _fill_row(ticker: str, quote: TradeQuote, session: date) -> dict[str, str]:
    return {
        "commission_gbp": format(quote.commission_gbp, "f"),
        "fund": ticker,
        "fx_cost_gbp": format(quote.fx_cost_gbp, "f"),
        "notional_gbp": format(quote.notional_gbp, "f"),
        "cash_delta_gbp": format(quote.cash_delta_gbp, "f"),
        "ocf_deducted_gbp": "0.00",
        "ocf_note": OCF_NOTE,
        "price_gbp": format(quote.exec_price_gbp, "f"),
        "session": session.isoformat(),
        "side": quote.side,
        "slippage_gbp": format(quote.slippage_cost_gbp, "f"),
        "spread_gbp": format(quote.spread_cost_gbp, "f"),
        "stamp_gbp": format(quote.stamp_duty_gbp, "f"),
        "units": format(quote.quantity, "f"),
    }


def _guard_cash(book: Book) -> None:
    if book.cash < 0:
        raise ResearchError("cash is negative")

"""Ex-date receivable, payment-date cash, and delayed reinvestment."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sterling.research.bars import Bar, prepare_series
from sterling.research.calendar import xlon
from sterling.research.costs import CostModel
from sterling.research.distributions import PAY_LAG_SESSIONS, payment_session, xstr_pay_dates
from sterling.research.engine import _Book, _distributions, _rebalance, _run_path
from sterling.research.instruments import Instrument
from sterling.research.risk_config import load_research_limits


def _flat(day: date, close: str, dividend: str | None = None, pay: date | None = None) -> Bar:
    price = Decimal(close)
    return Bar(
        session=day,
        open=price,
        high=price,
        low=price,
        close=price,
        dividend=None if dividend is None else Decimal(dividend),
        quoted_in="GBP",
        pay_date=pay,
    )


def test_missing_pay_date_waits_fifteen_sessions() -> None:
    calendar = xlon()
    ex = date(2024, 6, 3)
    paid = payment_session(calendar, ex, None)
    cursor = ex
    for _ in range(PAY_LAG_SESSIONS):
        cursor = calendar.next_trading_day(cursor)
    assert paid == cursor
    assert paid > ex


def test_ex_date_nav_keeps_the_receivable_and_reinvestment_waits() -> None:
    calendar = xlon()
    days = [
        date(2024, 6, 3),
        date(2024, 6, 4),
        date(2024, 6, 5),
        date(2024, 6, 6),
        date(2024, 6, 7),
    ]
    bars = [
        _flat(days[0], "100"),
        _flat(days[1], "80", "20", date(2024, 6, 6)),
        _flat(days[2], "80"),
        _flat(days[3], "80"),
        _flat(days[4], "80"),
    ]
    prepared = prepare_series(
        ticker="T",
        distribution="distributing",
        trading_currency="GBP",
        bars=bars,
        calendar=calendar,
    )
    assert prepared.dividend_mode == "reinvest"
    instrument = Instrument(
        ticker="T",
        instrument_type="UCITS_ETF",
        product_class="ucits_etf_synthetic",
        asset_class="cash_proxy",
        trading_currency="GBP",
        stamp_duty_exempt=True,
        distribution="distributing",
        calendar_id="XLON",
    )
    limits = load_research_limits()
    rows, fills, _proposals, _decisions, _cost, _turnover = _run_path(
        prepared={"T": prepared},
        instruments={"T": instrument},
        calendar=calendar,
        model=CostModel(),
        limits=limits,
        starting_nav=Decimal("1000"),
        history_start=days[0],
        evaluate_start=days[0],
        evaluate_end=days[-1],
        strategy=None,
        static_weights={"T": Decimal("1")},
        apply_gate=False,
    )
    by_day = {row.session: row for row in rows}
    ex_row = by_day[days[1]]
    assert ex_row.receivable_gbp == Decimal("200.00")
    assert ex_row.cash_gbp == Decimal("0.00")
    assert ex_row.nav_gbp == Decimal("1000.00")
    gap = by_day[days[2]]
    assert gap.receivable_gbp == Decimal("200.00")
    assert gap.nav_gbp == Decimal("1000.00")
    paid = by_day[days[3]]
    assert paid.receivable_gbp == Decimal("0.00")
    assert paid.cash_gbp == Decimal("200.00")
    assert paid.nav_gbp == Decimal("1000.00")
    reinvested = by_day[days[4]]
    assert reinvested.receivable_gbp == Decimal("0.00")
    assert reinvested.cash_gbp == Decimal("0.00")
    assert reinvested.nav_gbp == Decimal("1000.00")
    buy_days = [fill.session for fill in fills if fill.side == "BUY"]
    assert days[1] not in buy_days
    assert days[2] not in buy_days
    assert days[3] not in buy_days
    assert days[4] in buy_days
    book = _Book(cash=Decimal("0"), quantity={"T": Decimal("10")})
    _distributions(book, {"T": prepared}, days[1], {"T": instrument}, CostModel(), calendar, {"T": Decimal("80")})
    blocked, _turnover = _rebalance(
        book,
        {"T": Decimal("1")},
        {"T": Decimal("80")},
        {"T": instrument},
        CostModel(),
        Decimal("0"),
        days[1],
        None,
    )
    assert book.quantity["T"] == Decimal("10")
    assert book.cash == Decimal("0")
    assert book.receivable() == Decimal("200.00")
    assert blocked == []


def test_known_xstr_pay_date_is_the_issuer_date() -> None:
    pays = xstr_pay_dates()
    assert pays[date(2024, 8, 21)] == date(2024, 9, 5)
    assert pays[date(2020, 6, 17)] == date(2020, 6, 22)

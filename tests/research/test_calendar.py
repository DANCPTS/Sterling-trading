"""XLON sessions against England and Wales bank holidays."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sterling.research.calendar import england_wales_bank_holidays, xlon
from sterling.research.review import classify_trailing_equity

# Dates published by GOV.UK for England and Wales, retrieved 2026-11-09.
# https://www.gov.uk/bank-holidays.json
_GOVUK_2024 = frozenset(
    {
        date(2024, 1, 1),
        date(2024, 3, 29),
        date(2024, 4, 1),
        date(2024, 5, 6),
        date(2024, 5, 27),
        date(2024, 8, 26),
        date(2024, 12, 25),
        date(2024, 12, 26),
    }
)


def test_2024_matches_govuk_and_easter_monday_is_closed() -> None:
    assert england_wales_bank_holidays(2024) == _GOVUK_2024
    calendar = xlon()
    assert calendar.is_trading_day(date(2024, 4, 1)) is False
    assert calendar.is_trading_day(date(2024, 3, 29)) is False
    assert calendar.is_trading_day(date(2024, 3, 28)) is True
    assert calendar.next_trading_day(date(2024, 3, 28)) == date(2024, 4, 2)


def test_proclaimed_one_offs_and_moves() -> None:
    calendar = xlon()
    closed = (
        date(2011, 4, 29),
        date(2012, 6, 4),
        date(2012, 6, 5),
        date(2020, 5, 8),
        date(2022, 6, 2),
        date(2022, 6, 3),
        date(2022, 9, 19),
        date(2023, 5, 8),
    )
    opened_by_the_move = (
        date(2012, 5, 28),
        date(2020, 5, 4),
        date(2022, 5, 30),
    )
    for day in closed:
        assert calendar.is_trading_day(day) is False
    for day in opened_by_the_move:
        assert calendar.is_trading_day(day) is True
    # 2023-05-01 stayed the early-May holiday; the coronation was an extra day.
    assert calendar.is_trading_day(date(2023, 5, 1)) is False


def test_equity_regimes_are_fixed_thresholds() -> None:
    assert classify_trailing_equity(Decimal("0.10")) == "sideways"
    assert classify_trailing_equity(Decimal("0.1000001")) == "up"
    assert classify_trailing_equity(Decimal("-0.10")) == "sideways"
    assert classify_trailing_equity(Decimal("-0.1000001")) == "down"

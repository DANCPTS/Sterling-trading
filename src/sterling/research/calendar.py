"""London Stock Exchange session calendar for research.

Sessions are weekdays that are not England and Wales bank holidays.
The holiday rules are the standard set (New Year, Good Friday, Easter Monday,
early May, spring, summer, Christmas, and Boxing Day, with weekend substitute
days) plus the proclaimed one-offs and moves below. Dates for 2019-2028 match
the GOV.UK bank-holidays feed (``england-and-wales``), retrieved 2026-11-09
from https://www.gov.uk/bank-holidays.json. Earlier one-offs match GOV.UK and
the 2012 Diamond Jubilee impact assessment:

- 2011-04-29 royal wedding, an extra Friday.
- 2012 late-May holiday moved to 2012-06-04, plus 2012-06-05.
- 2020 early-May holiday moved from the Monday to Friday 2020-05-08.
- 2022 spring holiday moved to 2022-06-02, plus 2022-06-03, and the
  2022-09-19 state funeral.
- 2023-05-08 coronation, in addition to the early-May Monday.

This is a full-day session calendar. It does not model early closes.
``xlon_weekday`` remains available for tests that want Mondays to Fridays only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from sterling.research.errors import ResearchError

LONDON = ZoneInfo("Europe/London")


def _last_day(year: int, month: int) -> date:
    if month == 12:
        return date(year + 1, 1, 1) - timedelta(days=1)
    return date(year, month + 1, 1) - timedelta(days=1)


def add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    return date(year, month, min(day.day, _last_day(year, month).day))


@dataclass(frozen=True, slots=True)
class WeekdayCalendar:
    calendar_id: str
    open_time: time
    close_time: time
    timezone_name: str
    holidays: frozenset[date] = frozenset()

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def trading_days(self, start: date, end: date) -> tuple[date, ...]:
        if end < start:
            raise ResearchError("calendar end is before start")
        days: list[date] = []
        cursor = start
        while cursor <= end:
            if self.is_trading_day(cursor):
                days.append(cursor)
            cursor += timedelta(days=1)
        return tuple(days)

    def next_trading_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        for _ in range(21):
            if self.is_trading_day(cursor):
                return cursor
            cursor += timedelta(days=1)
        raise ResearchError(f"no trading day within 21 days after {day.isoformat()}")

    def month_ends(self, start: date, end: date) -> tuple[date, ...]:
        """Last trading day of each month that falls inside the inclusive range."""
        ends: list[date] = []
        year, month = start.year, start.month
        while date(year, month, 1) <= end:
            cursor = _last_day(year, month)
            month_start = date(year, month, 1)
            while cursor >= month_start:
                if self.is_trading_day(cursor):
                    if start <= cursor <= end:
                        ends.append(cursor)
                    break
                cursor -= timedelta(days=1)
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
        return tuple(ends)

    def session_close_utc(self, day: date) -> datetime:
        if not self.is_trading_day(day):
            raise ResearchError(f"{day.isoformat()} is not a trading day")
        local = datetime.combine(day, self.close_time, tzinfo=ZoneInfo(self.timezone_name))
        return local.astimezone(UTC)


def xlon_weekday(holidays: frozenset[date] | None = None) -> WeekdayCalendar:
    """Monday-to-Friday calendar with no bank holidays.

    Production research runs use :func:`xlon`. This helper is for tests that
    need a plain weekday grid.
    """
    return WeekdayCalendar(
        calendar_id="XLON_WEEKDAY",
        open_time=time(8, 0),
        close_time=time(16, 30),
        timezone_name="Europe/London",
        holidays=holidays or frozenset(),
    )


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_monday(year: int, month: int, *, last: bool) -> date:
    if last:
        cursor = _last_day(year, month)
        while cursor.weekday() != 0:
            cursor -= timedelta(days=1)
        return cursor
    cursor = date(year, month, 1)
    while cursor.weekday() != 0:
        cursor += timedelta(days=1)
    return cursor


def _weekend_substitute(day: date) -> date:
    if day.weekday() == 5:
        return day + timedelta(days=2)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


# Removes the ordinary holiday and adds the proclaimed date. Years not listed
# keep the ordinary rule. 2012 and 2022 move the spring holiday. 2020 moves
# early May. 2011 and 2023 add a day without removing the ordinary Monday.
_HOLIDAY_REVISIONS: dict[int, tuple[tuple[date, ...], tuple[date, ...]]] = {
    2011: ((), (date(2011, 4, 29),)),
    2012: ((date(2012, 5, 28),), (date(2012, 6, 4), date(2012, 6, 5))),
    2020: ((date(2020, 5, 4),), (date(2020, 5, 8),)),
    2022: ((date(2022, 5, 30),), (date(2022, 6, 2), date(2022, 6, 3), date(2022, 9, 19))),
    2023: ((), (date(2023, 5, 8),)),
}


def england_wales_bank_holidays(year: int) -> frozenset[date]:
    """Full-day England and Wales bank holidays for one year."""
    easter = easter_sunday(year)
    christmas = date(year, 12, 25)
    boxing = date(year, 12, 26)
    christmas_off = _weekend_substitute(christmas)
    boxing_off = _weekend_substitute(boxing)
    if boxing_off == christmas_off:
        boxing_off += timedelta(days=1)
    holidays = {
        _weekend_substitute(date(year, 1, 1)),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        _nth_monday(year, 5, last=False),
        _nth_monday(year, 5, last=True),
        _nth_monday(year, 8, last=True),
        christmas_off,
        boxing_off,
    }
    remove, add = _HOLIDAY_REVISIONS.get(year, ((), ()))
    holidays.difference_update(remove)
    holidays.update(add)
    return frozenset(holidays)


def bank_holidays_between(start: date, end: date) -> frozenset[date]:
    found: set[date] = set()
    for year in range(start.year, end.year + 1):
        for holiday in england_wales_bank_holidays(year):
            if start <= holiday <= end:
                found.add(holiday)
    return frozenset(found)


@lru_cache(maxsize=32)
def xlon(start: date | None = None, end: date | None = None) -> WeekdayCalendar:
    """LSE full-day sessions from England and Wales bank holidays.

    The default span covers 1995 through 2035 so a research window does not
    need to pass dates. Pass ``start`` and ``end`` to limit the stored set.
    The same arguments return the same calendar. Publication-lag checks call
    this once per SONIA print, and rebuilding 1995–2035 on every print made
    a multi-year accrual unusable. The holiday set is a frozenset.
    """
    first = start or date(1995, 1, 1)
    last = end or date(2035, 12, 31)
    return WeekdayCalendar(
        calendar_id="XLON",
        open_time=time(8, 0),
        close_time=time(16, 30),
        timezone_name="Europe/London",
        holidays=bank_holidays_between(first, last),
    )

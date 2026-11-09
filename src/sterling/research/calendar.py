"""Weekday session calendar for research.

``XLON_WEEKDAY`` is a Monday-to-Friday placeholder. It is not the official
London Stock Exchange holiday calendar. Callers can pass extra holidays.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
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
        for _ in range(14):
            if self.is_trading_day(cursor):
                return cursor
            cursor += timedelta(days=1)
        raise ResearchError(f"no trading day within 14 days after {day.isoformat()}")

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
    return WeekdayCalendar(
        calendar_id="XLON_WEEKDAY",
        open_time=time(8, 0),
        close_time=time(16, 30),
        timezone_name="Europe/London",
        holidays=holidays or frozenset(),
    )

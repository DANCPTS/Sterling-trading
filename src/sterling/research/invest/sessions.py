"""LSE open timestamps. Fills may use only an open strictly after the decision.

The official open used here is 08:00 Europe/London on an XLON session.
The clock follows British Summer Time. Weekend days and England and Wales
bank holidays are not sessions.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sterling.research.calendar import LONDON, xlon
from sterling.research.errors import ResearchError


def session_open(day: date) -> datetime | None:
    """08:00 London on a trading day, or None when the exchange is closed."""
    calendar = xlon()
    if not calendar.is_trading_day(day):
        return None
    return datetime(day.year, day.month, day.day, calendar.open_time.hour, calendar.open_time.minute, tzinfo=LONDON)


def first_session_after(decision_at: datetime) -> date:
    """First XLON session whose 08:00 London open is strictly after ``decision_at``."""
    if decision_at.tzinfo is None:
        raise ResearchError("decision time needs a timezone")
    start = decision_at.astimezone(LONDON).date() - timedelta(days=1)
    cursor = start
    for _ in range(21):
        cursor += timedelta(days=1)
        opened = session_open(cursor)
        if opened is not None and opened > decision_at:
            return cursor
    raise ResearchError("no XLON open within 21 days after the decision")

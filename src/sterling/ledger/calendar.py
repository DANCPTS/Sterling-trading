"""London civil dates for the desk day boundary.

The boundary is 00:00 Europe/London. Timestamps are absolute instants.
The London date is the civil date of that instant, so the October fallback
hour stays on one day and the March spring-forward gap does not move midnight.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sterling.errors import InvalidEvent

LONDON = ZoneInfo("Europe/London")


def require_aware(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise InvalidEvent("timestamp must be timezone-aware")
    return timestamp


def london_date(timestamp: datetime) -> date:
    """Return the Europe/London civil date of an aware timestamp."""
    aware = require_aware(timestamp)
    return aware.astimezone(UTC).astimezone(LONDON).date()

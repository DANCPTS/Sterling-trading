"""When a paper-v2 price may be used.

Yahoo's daily chart does not carry a verified publication time. ``close_at`` on
those bars is the 16:30 Europe/London exchange close. That clock is not a
provider publication time, and it does not make the close available at 16:30
on the same session. Without a provider timestamp the close is assumed
available at 08:00 Europe/London on the next XLON session.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sterling.research.calendar import LONDON, xlon
from sterling.research.errors import ResearchError
from sterling.research.invest.sessions import session_open

BASIS_VERIFIED = "source-verified"
BASIS_ASSUMED = "assumed-next-session"
BASIS_SYNTHETIC = "synthetic"


def exchange_close_at(session: date) -> datetime:
    """16:30 Europe/London on ``session``, as UTC. This is not a publication time."""
    local = datetime(session.year, session.month, session.day, 16, 30, tzinfo=LONDON)
    return local.astimezone(UTC)


def assumed_available_at(session: date) -> datetime:
    """08:00 Europe/London on the next XLON session, as UTC."""
    opened = session_open(xlon().next_trading_day(session))
    if opened is None:
        raise ResearchError(f"no XLON open after {session.isoformat()}")
    return opened.astimezone(UTC)


def require_aware(moment: datetime, label: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ResearchError(f"{label} needs a timezone")
    return moment.astimezone(UTC)

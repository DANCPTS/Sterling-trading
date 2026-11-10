"""Clocks for forward decisions.

``SystemClock`` reads UTC at the moment of the call. Tests and the labelled
simulated-clock demo inject ``TickingClock``. A fixed clock that never
advances is not used for a forward run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from sterling.research.fx.grok_forward import require_utc

MODES = frozenset({"forward", "historical", "demo", "simulated_clock"})


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Genuine UTC. Each call reads the clock again."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class TickingClock:
    """Injectable clock. Each reading is strictly later than the previous one."""

    def __init__(self, start: datetime) -> None:
        self._moment = require_utc(start)

    def now(self) -> datetime:
        self._moment += timedelta(microseconds=1)
        return self._moment


def week_key(moment: datetime) -> str:
    day = require_utc(moment).date()
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def require_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"unknown decision mode {mode}")
    return mode

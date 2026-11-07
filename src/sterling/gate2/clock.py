"""Coordinator clock.

IPC messages do not supply this clock. ``FixtureClock`` exists so a test
that constructs the coordinator can freeze time. It is not read from a
socket payload, an environment variable, or the owner policy.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sterling.gate2.models import TrustedClock


class ClockSource(Protocol):
    """Time the coordinator itself observes."""

    def now(self) -> TrustedClock:
        """Return the current trusted instant."""


class WallClock:
    """Host wall clock and monotonic counter. Admin control of the host is out of scope."""

    def now(self) -> TrustedClock:
        from datetime import UTC
        from time import monotonic_ns

        return TrustedClock(as_of=datetime.now(UTC), monotonic_ns=monotonic_ns())


class FixtureClock:
    """In-process test clock. Callers on a socket cannot move it."""

    def __init__(self, as_of: datetime, monotonic_ns: int = 1) -> None:
        self._as_of = as_of
        self._monotonic_ns = monotonic_ns

    def set(self, as_of: datetime, monotonic_ns: int | None = None) -> None:
        self._as_of = as_of
        if monotonic_ns is not None:
            self._monotonic_ns = monotonic_ns

    def now(self) -> TrustedClock:
        return TrustedClock(as_of=self._as_of, monotonic_ns=self._monotonic_ns)

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {"as_of": self._as_of.isoformat(), "monotonic_ns": self._monotonic_ns},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> FixtureClock:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            monotonic_ns=int(payload["monotonic_ns"]),
        )

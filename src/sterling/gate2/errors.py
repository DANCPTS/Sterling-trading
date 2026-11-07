"""Errors for the Gate 2 trusted journal.

Business denials are returned as decisions. These types are for a store that
cannot safely continue and for a commit that did not succeed.
"""

from __future__ import annotations

from sterling.errors import SterlingError


class Gate2Error(SterlingError):
    """Base class for Gate 2 storage and protocol failures."""


class StoreBlocked(Gate2Error):
    """The journal failed closed. No empty replacement store is created."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StoreUnavailable(Gate2Error):
    """A commit, lock, or I/O failure. The caller received no success."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ProtocolError(Gate2Error):
    """A local message could not be parsed. Nothing was reserved."""

"""Error types for Stage 1.

Live trading and raised ceilings have their own types so callers can tell a
policy rejection from a malformed file.
"""

from __future__ import annotations


class SterlingError(Exception):
    """Base class for errors raised by this package."""


class LiveTradingRejected(SterlingError):
    """Live trading cannot be enabled in Stage 1."""


class CeilingRaised(SterlingError):
    """An absolute ceiling is above the owner-approved amount."""


class RulesValidationError(SterlingError):
    """The rules document failed schema or approved-content checks."""


class RulesHashMismatch(SterlingError):
    """The rules content hash does not match the document or the approved pin."""


class UnapprovedRulesVersion(SterlingError):
    """The rules version is not in the owner-approved registry."""


class ConfigError(SterlingError):
    """Stage configuration is missing a required field or is the wrong type."""


class MoneyError(SterlingError):
    """A money, quantity, price, or FX value is not an allowed Decimal."""


class LedgerRejected(SterlingError):
    """A ledger event was rejected. The log is unchanged."""


class NegativeCashRejected(LedgerRejected):
    """The event would make GBP cash negative."""


class OversellRejected(LedgerRejected):
    """The event would sell more than the settled long quantity."""


class IdempotencyConflict(LedgerRejected):
    """The event id was reused with a different payload."""


class InvalidEvent(LedgerRejected):
    """The event is structurally invalid."""


class OutOfOrderTimestamp(LedgerRejected):
    """Timestamps in the log must be non-decreasing in absolute time."""


class ReversalRejected(LedgerRejected):
    """The reversing correction cannot be applied cleanly."""


class PersistenceError(SterlingError):
    """The ledger file is unreadable or does not match its checksum."""

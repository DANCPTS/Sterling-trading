"""Append-only ledger events.

Money fields are Decimal. Factories reject floats. Each event is immutable and
compares equal when it posts the same economics, which is what idempotency uses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sterling.errors import InvalidEvent, MoneyError, PersistenceError
from sterling.ledger.calendar import london_date, require_aware
from sterling.ledger.money import (
    decimal_to_str,
    money_to_str,
    require_fx_rate,
    require_non_negative_price,
    require_positive_money,
    require_positive_price,
    require_positive_quantity,
)

FEE_TYPES = ("commission", "fx_fee", "stamp_duty")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_INSTRUMENT = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def check_event_id(event_id: str) -> str:
    if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
        raise InvalidEvent("event_id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
    return event_id


def check_instrument(instrument_id: str) -> str:
    if not isinstance(instrument_id, str) or _INSTRUMENT.fullmatch(instrument_id) is None:
        raise InvalidEvent("instrument_id must be 1-32 characters of A-Z, 0-9, '.', '_' or '-'")
    return instrument_id


def check_currency(currency: str) -> str:
    if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
        raise InvalidEvent("currency must be three ASCII letters")
    return currency


@dataclass(frozen=True, slots=True)
class DepositEvent:
    event_id: str
    timestamp: datetime
    amount_gbp: Decimal

    def __post_init__(self) -> None:
        check_event_id(self.event_id)
        require_aware(self.timestamp)
        object.__setattr__(
            self,
            "amount_gbp",
            require_positive_money(self.amount_gbp, label="amount_gbp"),
        )


@dataclass(frozen=True, slots=True)
class WithdrawalEvent:
    event_id: str
    timestamp: datetime
    amount_gbp: Decimal

    def __post_init__(self) -> None:
        check_event_id(self.event_id)
        require_aware(self.timestamp)
        object.__setattr__(
            self,
            "amount_gbp",
            require_positive_money(self.amount_gbp, label="amount_gbp"),
        )


@dataclass(frozen=True, slots=True)
class FillEvent:
    """A fill in instrument currency. Cash and inventory move on the FX event."""

    event_id: str
    timestamp: datetime
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
    instrument_currency: str

    def __post_init__(self) -> None:
        check_event_id(self.event_id)
        require_aware(self.timestamp)
        check_instrument(self.instrument_id)
        if self.side not in ("buy", "sell"):
            raise InvalidEvent("fill side must be buy or sell")
        object.__setattr__(
            self,
            "quantity",
            require_positive_quantity(self.quantity, label="quantity"),
        )
        object.__setattr__(self, "price", require_positive_price(self.price, label="price"))
        check_currency(self.instrument_currency)


@dataclass(frozen=True, slots=True)
class FxEvent:
    """Settle one fill into GBP at a caller-supplied rate. Nothing is fetched."""

    event_id: str
    timestamp: datetime
    fill_id: str
    fx_rate_to_gbp: Decimal

    def __post_init__(self) -> None:
        check_event_id(self.event_id)
        require_aware(self.timestamp)
        check_event_id(self.fill_id)
        object.__setattr__(
            self,
            "fx_rate_to_gbp",
            require_fx_rate(self.fx_rate_to_gbp, label="fx_rate_to_gbp"),
        )


@dataclass(frozen=True, slots=True)
class FeeEvent:
    """An explicit cost line: commission, FX fee, or stamp duty / SDRT."""

    event_id: str
    timestamp: datetime
    fee_type: str
    amount: Decimal
    currency: str
    fx_rate_to_gbp: Decimal
    related_event_id: str | None = None

    def __post_init__(self) -> None:
        check_event_id(self.event_id)
        require_aware(self.timestamp)
        if self.fee_type not in FEE_TYPES:
            raise InvalidEvent("fee_type must be commission, fx_fee, or stamp_duty")
        object.__setattr__(self, "amount", require_positive_quantity(self.amount, label="amount"))
        check_currency(self.currency)
        rate = require_fx_rate(self.fx_rate_to_gbp, label="fx_rate_to_gbp")
        object.__setattr__(self, "fx_rate_to_gbp", rate)
        if self.currency == "GBP" and rate != Decimal("1"):
            raise InvalidEvent("GBP fees require fx_rate_to_gbp of 1")
        if self.related_event_id is not None:
            check_event_id(self.related_event_id)


@dataclass(frozen=True, slots=True)
class MarkEvent:
    """Mark a settled position. Price is in instrument currency.

    ``source`` names who supplied the mark. It is metadata. A source, including
    the default ``unspecified``, is not permission to trade.
    """

    event_id: str
    timestamp: datetime
    instrument_id: str
    price: Decimal
    fx_rate_to_gbp: Decimal
    source: str = "unspecified"

    def __post_init__(self) -> None:
        check_event_id(self.event_id)
        require_aware(self.timestamp)
        check_instrument(self.instrument_id)
        object.__setattr__(self, "price", require_non_negative_price(self.price, label="price"))
        object.__setattr__(
            self,
            "fx_rate_to_gbp",
            require_fx_rate(self.fx_rate_to_gbp, label="fx_rate_to_gbp"),
        )
        if not isinstance(self.source, str) or not self.source.strip() or len(self.source) > 64:
            raise InvalidEvent("mark source must be a non-empty string of at most 64 characters")


@dataclass(frozen=True, slots=True)
class ReversalEvent:
    """Append-only correcting entry. The original event stays in the log."""

    event_id: str
    timestamp: datetime
    reverses_event_id: str
    reason: str

    def __post_init__(self) -> None:
        check_event_id(self.event_id)
        require_aware(self.timestamp)
        check_event_id(self.reverses_event_id)
        if self.event_id == self.reverses_event_id:
            raise InvalidEvent("a reversal cannot name itself")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise InvalidEvent("reversal reason must be a non-empty string")
        if len(self.reason) > 200:
            raise InvalidEvent("reversal reason must be at most 200 characters")


@dataclass(frozen=True, slots=True)
class EquitySnapshotEvent:
    """First validated equity snapshot of a London day. The id is deterministic."""

    event_id: str
    timestamp: datetime

    def __post_init__(self) -> None:
        check_event_id(self.event_id)
        require_aware(self.timestamp)
        expected = snapshot_id(london_date(self.timestamp))
        if self.event_id != expected:
            raise InvalidEvent(f"equity snapshot id must be {expected} for its Europe/London date")


LedgerEvent = DepositEvent | WithdrawalEvent | FillEvent | FxEvent | FeeEvent | MarkEvent | ReversalEvent | EquitySnapshotEvent


def snapshot_id(day: date) -> str:
    return f"day-baseline-{day.isoformat()}"


def equity_snapshot(day: date, timestamp: datetime) -> EquitySnapshotEvent:
    if london_date(timestamp) != day:
        raise InvalidEvent("snapshot timestamp falls on a different Europe/London date")
    return EquitySnapshotEvent(event_id=snapshot_id(day), timestamp=timestamp)


def event_to_dict(event: LedgerEvent) -> dict[str, object]:
    if isinstance(event, DepositEvent):
        return {
            "event_type": "deposit",
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "amount_gbp": money_to_str(event.amount_gbp),
        }
    if isinstance(event, WithdrawalEvent):
        return {
            "event_type": "withdrawal",
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "amount_gbp": money_to_str(event.amount_gbp),
        }
    if isinstance(event, FillEvent):
        return {
            "event_type": "fill",
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "instrument_id": event.instrument_id,
            "side": event.side,
            "quantity": decimal_to_str(event.quantity),
            "price": decimal_to_str(event.price),
            "instrument_currency": event.instrument_currency,
        }
    if isinstance(event, FxEvent):
        return {
            "event_type": "fx",
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "fill_id": event.fill_id,
            "fx_rate_to_gbp": decimal_to_str(event.fx_rate_to_gbp),
        }
    if isinstance(event, FeeEvent):
        return {
            "event_type": "fee",
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "fee_type": event.fee_type,
            "amount": decimal_to_str(event.amount),
            "currency": event.currency,
            "fx_rate_to_gbp": decimal_to_str(event.fx_rate_to_gbp),
            "related_event_id": event.related_event_id,
        }
    if isinstance(event, MarkEvent):
        return {
            "event_type": "mark",
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "instrument_id": event.instrument_id,
            "price": decimal_to_str(event.price),
            "fx_rate_to_gbp": decimal_to_str(event.fx_rate_to_gbp),
            "source": event.source,
        }
    if isinstance(event, ReversalEvent):
        return {
            "event_type": "reversal",
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "reverses_event_id": event.reverses_event_id,
            "reason": event.reason,
        }
    return {
        "event_type": "equity_snapshot",
        "event_id": event.event_id,
        "timestamp": event.timestamp.isoformat(),
    }


def event_from_dict(data: Mapping[str, Any]) -> LedgerEvent:
    try:
        return _event_from_dict(data)
    except (KeyError, MoneyError, InvalidEvent) as exc:
        raise PersistenceError(f"unreadable event: {exc}") from exc


def _event_from_dict(data: Mapping[str, Any]) -> LedgerEvent:
    event_type = data["event_type"]
    event_id = data["event_id"]
    timestamp = _timestamp(data["timestamp"])
    if not isinstance(event_type, str) or not isinstance(event_id, str):
        raise PersistenceError("event_type and event_id must be strings")
    if event_type == "deposit":
        return DepositEvent(event_id, timestamp, _money(data, "amount_gbp"))
    if event_type == "withdrawal":
        return WithdrawalEvent(event_id, timestamp, _money(data, "amount_gbp"))
    if event_type == "fill":
        return FillEvent(
            event_id,
            timestamp,
            _required_str(data, "instrument_id"),
            _required_str(data, "side"),
            require_positive_quantity(_required_str(data, "quantity"), label="quantity"),
            require_positive_price(_required_str(data, "price"), label="price"),
            _required_str(data, "instrument_currency"),
        )
    if event_type == "fx":
        return FxEvent(
            event_id,
            timestamp,
            _required_str(data, "fill_id"),
            require_fx_rate(_required_str(data, "fx_rate_to_gbp")),
        )
    if event_type == "fee":
        related = data.get("related_event_id")
        if related is not None and not isinstance(related, str):
            raise PersistenceError("related_event_id must be a string or null")
        return FeeEvent(
            event_id,
            timestamp,
            _required_str(data, "fee_type"),
            require_positive_quantity(_required_str(data, "amount"), label="amount"),
            _required_str(data, "currency"),
            require_fx_rate(_required_str(data, "fx_rate_to_gbp")),
            related,
        )
    if event_type == "mark":
        source = data.get("source", "unspecified")
        if not isinstance(source, str):
            raise PersistenceError("mark source must be a string")
        return MarkEvent(
            event_id,
            timestamp,
            _required_str(data, "instrument_id"),
            require_non_negative_price(_required_str(data, "price"), label="price"),
            require_fx_rate(_required_str(data, "fx_rate_to_gbp")),
            source,
        )
    if event_type == "reversal":
        return ReversalEvent(
            event_id,
            timestamp,
            _required_str(data, "reverses_event_id"),
            _required_str(data, "reason"),
        )
    if event_type == "equity_snapshot":
        return EquitySnapshotEvent(event_id, timestamp)
    raise PersistenceError(f"unknown event_type {event_type!r}")


def _money(data: Mapping[str, Any], key: str) -> Decimal:
    return require_positive_money(_required_str(data, key), label=key)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise PersistenceError("timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PersistenceError("timestamp is not ISO-8601") from exc
    return require_aware(parsed)


def _required_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise PersistenceError(f"{key} must be a string")
    return value

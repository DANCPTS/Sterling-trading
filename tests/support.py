"""Shared constructors for synthetic ledger tests.

Instruments and prices here are labels and hand-written decimals, not market data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sterling.ledger.events import (
    DepositEvent,
    FeeEvent,
    FillEvent,
    FxEvent,
    MarkEvent,
    ReversalEvent,
    WithdrawalEvent,
)
from sterling.ledger.ledger import Ledger


def ts(
    year: int,
    month: int,
    day: int,
    hour: int = 12,
    minute: int = 0,
    second: int = 0,
    *,
    offset_hours: int = 0,
) -> datetime:
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        tzinfo=timezone(timedelta(hours=offset_hours)),
    )


def june(day: int, hour: int, minute: int = 0) -> datetime:
    """A 2026 June instant in British Summer Time (UTC+1)."""
    return ts(2026, 6, day, hour, minute, offset_hours=1)


def money(text: str) -> Decimal:
    return Decimal(text)


def settle(
    ledger: Ledger,
    *,
    event_id: str,
    timestamp: datetime,
    instrument_id: str,
    side: str,
    quantity: str,
    price: str,
    currency: str = "GBP",
    fx_rate: str = "1",
    fx_id: str | None = None,
) -> None:
    """Post a synthetic fill and its caller-supplied FX settlement."""
    ledger.post(
        FillEvent(
            event_id,
            timestamp,
            instrument_id,
            side,
            quantity,
            price,
            currency,
        )
    )
    ledger.post(
        FxEvent(
            fx_id or f"fx-{event_id}",
            timestamp,
            event_id,
            fx_rate,
        )
    )


def fee(
    ledger: Ledger,
    *,
    event_id: str,
    timestamp: datetime,
    fee_type: str,
    amount: str,
    currency: str = "GBP",
    fx_rate: str = "1",
    related_event_id: str | None = None,
) -> None:
    ledger.post(
        FeeEvent(
            event_id,
            timestamp,
            fee_type,
            amount,
            currency,
            fx_rate,
            related_event_id,
        )
    )


def mark(
    ledger: Ledger,
    *,
    event_id: str,
    timestamp: datetime,
    instrument_id: str,
    price: str,
    fx_rate: str = "1",
    source: str = "unspecified",
) -> None:
    ledger.post(MarkEvent(event_id, timestamp, instrument_id, price, fx_rate, source))


def deposit(ledger: Ledger, event_id: str, timestamp: datetime, amount: str) -> None:
    ledger.post(DepositEvent(event_id, timestamp, amount))


def withdraw(ledger: Ledger, event_id: str, timestamp: datetime, amount: str) -> None:
    ledger.post(WithdrawalEvent(event_id, timestamp, amount))


def reversal(ledger: Ledger, event_id: str, timestamp: datetime, target: str, reason: str) -> None:
    ledger.post(ReversalEvent(event_id, timestamp, target, reason))

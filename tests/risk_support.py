"""Synthetic Gate 1 inputs.

Instrument ids, prices, and limits in this module are fixtures. They are not
the owner-approved allowlist and they are not approved quote or session policy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sterling.risk.contracts import (
    AccountInput,
    BaselineKind,
    BookPosition,
    ClockInput,
    EntryProposal,
    FixtureOperationalLimits,
    Freshness,
    FxQuoteInput,
    KnownStatus,
    PositionStatus,
    QuoteInput,
    SessionInput,
    StopProtection,
)

DAY = date(2026, 6, 1)
AS_OF = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)


def policy_clock(seconds_earlier: int = 0, *, previous: datetime | None = None, naive: bool = False) -> ClockInput:
    moment = AS_OF if not naive else datetime(2026, 6, 1, 10, 0)
    if seconds_earlier:
        moment = moment - timedelta(seconds=seconds_earlier)
    return ClockInput(as_of=moment, previous_as_of=previous)


def account(**overrides: object) -> AccountInput:
    values: dict[str, object] = {
        "currency": "GBP",
        "live": False,
        "simulated": True,
        "marked_equity_gbp": Decimal("500.00"),
        "equity_freshness": Freshness.FRESH_VALIDATED,
        "baseline_kind": BaselineKind.FIRST_FRESH_OPENING,
        "opening_equity_gbp": Decimal("500.00"),
        "opening_freshness": Freshness.FRESH_VALIDATED,
        "opening_london_date": DAY,
        "as_of_london_date": DAY,
        "net_external_deposits_gbp": Decimal("0.00"),
        "available_cash_gbp": Decimal("500.00"),
        "available_cash_status": KnownStatus.KNOWN,
        "gross_exposure_status": KnownStatus.KNOWN,
        "daily_halt_latched": False,
        "loss_streak_halt_latched": False,
    }
    values.update(overrides)
    return AccountInput(**values)  # type: ignore[arg-type]


def proposal(**overrides: object) -> EntryProposal:
    """£2.50 of planned risk and £10 of notional on SYNTH-EQ at the £500 anchor."""
    values: dict[str, object] = {
        "proposal_id": "P1",
        "episode_id": "E1",
        "instrument_id": "SYNTH-EQ",
        "instrument_currency": "GBP",
        "product": "LONG_EQUITY",
        "side": "BUY",
        "borrowing": False,
        "leverage": Decimal("1"),
        "opens_new_episode": True,
        "quantity": Decimal("1"),
        "quantity_precision": 0,
        "entry_price": Decimal("10.00"),
        "stop_price": Decimal("7.50"),
        "stop_protection": StopProtection.FULL,
        "covered_quantity": Decimal("1"),
        "fx_rate_to_gbp": Decimal("1"),
        "conservative_price": Decimal("10.00"),
        "conservative_fx_rate_to_gbp": Decimal("1"),
        "expected_entry_cost_gbp": Decimal("0.00"),
        "expected_exit_cost_gbp": Decimal("0.00"),
        "adverse_allowance_gbp": Decimal("0.00"),
    }
    values.update(overrides)
    return EntryProposal(**values)  # type: ignore[arg-type]


def open_slice(
    episode_id: str,
    *,
    instrument_id: str = "SYNTH-EQ",
    quantity: str = "1",
    precision: int = 0,
    entry: str = "10.00",
    mark: str = "10.00",
    stop: str = "9.90",
    conservative: str | None = None,
    status: PositionStatus = PositionStatus.OPEN,
    booked_fee: str = "0.00",
    exit_cost: str = "0.00",
    adverse: str = "0.00",
    unbooked_entry: str = "0.00",
    currency: str = "GBP",
    fx: str = "1",
) -> BookPosition:
    price = conservative if conservative is not None else mark if status == PositionStatus.OPEN else entry
    return BookPosition(
        episode_id=episode_id,
        instrument_id=instrument_id,
        instrument_currency=currency,
        status=status,
        quantity=Decimal(quantity),
        quantity_precision=precision,
        entry_price=Decimal(entry),
        mark_price=None if status == PositionStatus.PENDING_OPEN else Decimal(mark),
        stop_price=Decimal(stop),
        stop_protection=StopProtection.FULL,
        covered_quantity=Decimal(quantity),
        fx_rate_to_gbp=Decimal(fx),
        conservative_price=Decimal(price),
        conservative_fx_rate_to_gbp=Decimal(fx),
        unbooked_entry_cost_gbp=Decimal(unbooked_entry),
        expected_exit_cost_gbp=Decimal(exit_cost),
        adverse_allowance_gbp=Decimal(adverse),
        booked_entry_fees_gbp=Decimal(booked_fee),
    )


def flat_slice(episode_id: str, instrument_id: str = "SYNTH-EQ") -> BookPosition:
    return BookPosition(
        episode_id=episode_id,
        instrument_id=instrument_id,
        instrument_currency="GBP",
        status=PositionStatus.FLAT,
        quantity=Decimal("0"),
        quantity_precision=0,
        entry_price=None,
        mark_price=None,
        stop_price=None,
        stop_protection=StopProtection.MISSING,
        covered_quantity=None,
        fx_rate_to_gbp=None,
        conservative_price=None,
        conservative_fx_rate_to_gbp=None,
        unbooked_entry_cost_gbp=Decimal("0"),
        expected_exit_cost_gbp=Decimal("0"),
        adverse_allowance_gbp=Decimal("0"),
        booked_entry_fees_gbp=Decimal("0"),
    )


def fixture_limits(max_age_seconds: int) -> FixtureOperationalLimits:
    """Isolated test limits. Not owner approval and not a runtime policy."""
    return FixtureOperationalLimits(
        owner_approved=False,
        label="NOT_OWNER_APPROVED_FIXTURE",
        quote_max_age_seconds=max_age_seconds,
        fx_max_age_seconds=max_age_seconds,
        quote_sources=frozenset({"FIXTURE-SOURCE"}),
        fx_sources=frozenset({"FIXTURE-FX"}),
        quality_labels=frozenset({"FIXTURE-QUALITY"}),
        calendar_id="FIXTURE-CAL",
        session_id="FIXTURE-SESSION",
    )


def fresh_quote(max_age_seconds: int) -> QuoteInput:
    return QuoteInput(
        bid=Decimal("10.00"),
        ask=Decimal("10.01"),
        observed_at=AS_OF - timedelta(seconds=max_age_seconds),
        source="FIXTURE-SOURCE",
        quality="FIXTURE-QUALITY",
    )


def fresh_fx(max_age_seconds: int) -> FxQuoteInput:
    return FxQuoteInput(
        rate_to_gbp=Decimal("1"),
        observed_at=AS_OF - timedelta(seconds=max_age_seconds),
        source="FIXTURE-FX",
    )


def fresh_session() -> SessionInput:
    return SessionInput(calendar_id="FIXTURE-CAL", session_id="FIXTURE-SESSION", in_session=True)

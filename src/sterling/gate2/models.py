"""Request and result records for Gate 2.

A reservation result is a budget hold. It is not an executable ticket.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from sterling.gate2.errors import ProtocolError
from sterling.risk.contracts import EntryProposal, FixtureOperationalLimits
from sterling.risk.errors import RiskInputError


class Outcome(StrEnum):
    RESERVED = "RESERVED"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"


class HaltName(StrEnum):
    DAILY_LOSS = "DAILY_LOSS"
    LOSS_STREAK = "LOSS_STREAK"
    DATA_UNTRUSTED = "DATA_UNTRUSTED"
    INTEGRITY = "INTEGRITY"
    RECONCILIATION = "RECONCILIATION"
    OWNER_PAUSE = "OWNER_PAUSE"


_FORBIDDEN_GENERATION_KEYS = frozenset(
    {
        "ai_approved",
        "approved",
        "trusted",
        "owner",
        "execution_permission",
        "ticket",
    }
)


@dataclass(frozen=True, slots=True)
class TrustedClock:
    """Injected trusted time. Tests do not sleep for ordering."""

    as_of: datetime
    monotonic_ns: int

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ProtocolError("trusted clock must be timezone-aware")
        if isinstance(self.monotonic_ns, bool) or not isinstance(self.monotonic_ns, int) or self.monotonic_ns < 0:
            raise ProtocolError("monotonic_ns must be a non-negative int")


@dataclass(frozen=True, slots=True)
class StopEvidence:
    stop_price: Decimal
    covered_quantity: Decimal


@dataclass(frozen=True, slots=True)
class HoldingQuote:
    """Quote evidence for one instrument. ``ACCOUNTING_SAME_DAY`` is rejected later."""

    bid: Decimal
    ask: Decimal
    mark: Decimal
    observed_at: datetime
    source: str
    quality: str


@dataclass(frozen=True, slots=True)
class FxEvidence:
    rate_to_gbp: Decimal
    observed_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class GenerationInput:
    """Trusted feeder evidence. Unknown authority keys are refused."""

    source_id: str
    source_sequence: int
    ledger_path: str
    as_of: datetime
    receipt_time: datetime
    calendar_id: str
    calendar_version: str
    session_id: str
    in_session: bool
    verified_trading_day: bool
    is_opening_snapshot: bool
    opening_claim: str
    quotes: dict[str, HoldingQuote]
    fx: dict[str, FxEvidence]
    holding_stops: dict[str, StopEvidence]
    pending_stops: dict[str, StopEvidence]
    reported_cash_gbp: Decimal | None
    reported_equity_gbp: Decimal | None
    reported_exposure_gbp: Decimal | None
    costs_final: bool
    ambiguous_fill_ids: tuple[str, ...]
    fixture_limits: FixtureOperationalLimits | None

    def __post_init__(self) -> None:
        if self.opening_claim not in {"FIRST_FRESH_OPENING", "NONE"}:
            raise ProtocolError("opening_claim must be FIRST_FRESH_OPENING or NONE")
        if isinstance(self.source_sequence, bool) or not isinstance(self.source_sequence, int) or self.source_sequence < 1:
            raise ProtocolError("source_sequence must be a positive int")


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """Untrusted proposal. Authority claims are refused rather than honoured."""

    request_id: str
    principal: str
    account_id: str
    environment: str
    proposal: EntryProposal
    claimed_owner: bool = False
    claimed_approved: bool = False
    claimed_execution: bool = False
    policy_override: tuple[tuple[str, str], ...] = ()
    signature: str | None = None

    def authority_claimed(self) -> bool:
        return bool(
            self.claimed_owner
            or self.claimed_approved
            or self.claimed_execution
            or self.policy_override
            or (self.signature is not None and self.signature != "")
        )


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """Durable decision. Permission bits are fixed false."""

    outcome: Outcome
    reasons: tuple[str, ...]
    reservation_id: str | None
    reservation_state: str | None
    replayed: bool
    execution_permission_issued: Literal[False] = False
    ticket_issued: Literal[False] = False
    non_executable: Literal[True] = True
    token: None = None
    chain_seq: int | None = None

    def __post_init__(self) -> None:
        if self.execution_permission_issued is not False or self.ticket_issued is not False or self.token is not None:
            raise ProtocolError("a Gate 2 decision cannot grant execution")
        if self.non_executable is not True:
            raise ProtocolError("a Gate 2 reservation is non-executable")


@dataclass(frozen=True, slots=True)
class ResetRequest:
    account_id: str
    environment: str
    halt_reason: HaltName
    reason: str
    target_london_date: str | None
    expected_generation_id: str
    expected_policy_hash: str
    expected_chain_seq: int
    nonce: str
    expires_at: datetime
    reviewed_reconciliation: bool = False


def reject_generation_document(document: dict[str, object]) -> None:
    """Refuse AI trust flags embedded in an ingress document."""
    extra = _FORBIDDEN_GENERATION_KEYS.intersection(document)
    if extra:
        raise ProtocolError("generation document contains an authority flag: " + ",".join(sorted(extra)))


def proposal_payload(request: AdmissionRequest) -> dict[str, object]:
    proposal = request.proposal
    try:
        quantity = format(proposal.quantity, "f")
        entry = format(proposal.entry_price, "f")
        stop = None if proposal.stop_price is None else format(proposal.stop_price, "f")
        fx = format(proposal.fx_rate_to_gbp, "f")
        conservative = format(proposal.conservative_price, "f")
        conservative_fx = format(proposal.conservative_fx_rate_to_gbp, "f")
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProtocolError("proposal decimals are unreadable") from exc
    return {
        "account_id": request.account_id,
        "environment": request.environment,
        "principal": request.principal,
        "request_id": request.request_id,
        "proposal_id": proposal.proposal_id,
        "episode_id": proposal.episode_id,
        "instrument_id": proposal.instrument_id,
        "instrument_currency": proposal.instrument_currency,
        "product": proposal.product,
        "side": proposal.side,
        "borrowing": proposal.borrowing,
        "leverage": format(proposal.leverage, "f") if isinstance(proposal.leverage, Decimal) else str(proposal.leverage),
        "opens_new_episode": proposal.opens_new_episode,
        "quantity": quantity,
        "quantity_precision": proposal.quantity_precision,
        "entry_price": entry,
        "stop_price": stop,
        "stop_protection": proposal.stop_protection.value,
        "covered_quantity": None if proposal.covered_quantity is None else format(proposal.covered_quantity, "f"),
        "fx_rate_to_gbp": fx,
        "conservative_price": conservative,
        "conservative_fx_rate_to_gbp": conservative_fx,
        "expected_entry_cost_gbp": format(proposal.expected_entry_cost_gbp, "f"),
        "expected_exit_cost_gbp": format(proposal.expected_exit_cost_gbp, "f"),
        "adverse_allowance_gbp": format(proposal.adverse_allowance_gbp, "f"),
    }


def ensure_risk_input(exc: RiskInputError) -> ProtocolError:
    return ProtocolError(f"{exc.reason}: {exc.detail}")

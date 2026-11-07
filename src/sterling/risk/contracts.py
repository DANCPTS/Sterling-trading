"""Immutable inputs and results for Stage 2 Gate 1.

These objects describe a proposal and the arithmetic outcome. Constructing
one does not reserve cash, latch a halt, or issue a ticket. Fields that
would grant permission are fixed false and rejected if forced true.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from sterling.risk.errors import RiskInputError
from sterling.risk.reasons import Reason


class Freshness(StrEnum):
    """How a supplied valuation may be used. Only FRESH_VALIDATED can size a budget."""

    FRESH_VALIDATED = "FRESH_VALIDATED"
    STALE = "STALE"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"
    PREVIOUS_CLOSE = "PREVIOUS_CLOSE"
    COST_BASIS = "COST_BASIS"


class BaselineKind(StrEnum):
    """What the caller claims the day-opening equity is."""

    FIRST_FRESH_OPENING = "FIRST_FRESH_OPENING"
    PREVIOUS_CLOSE = "PREVIOUS_CLOSE"
    COST_BASIS = "COST_BASIS"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"


class KnownStatus(StrEnum):
    """Whether a supplied cash or exposure figure is usable."""

    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class PositionStatus(StrEnum):
    """Flat-to-flat episode state supplied by the caller. Gate 1 does not fill."""

    OPEN = "OPEN"
    PENDING_OPEN = "PENDING_OPEN"
    FLAT = "FLAT"


class StopProtection(StrEnum):
    """Intended stop coverage. FULL is a schema claim, not proof of protection."""

    FULL = "FULL"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class SettingsApproval(StrEnum):
    """Owner policy has no approved quote, FX, or session numbers."""

    UNAPPROVED_UNCONFIGURED = "UNAPPROVED_UNCONFIGURED"
    FIXTURE_NOT_OWNER_APPROVED = "FIXTURE_NOT_OWNER_APPROVED"


class ResetActor(StrEnum):
    """Who a reset request claims to be. Only a later gate could authorise OWNER."""

    OWNER = "OWNER"
    AI = "AI"
    UNKNOWN = "UNKNOWN"
    SYSTEM = "SYSTEM"


class HaltReason(StrEnum):
    """Independent halt reasons. Naming one does not clear the other."""

    DAILY_LOSS = "DAILY_LOSS"
    LOSS_STREAK = "LOSS_STREAK"


BOUNDARY_STATEMENTS: tuple[str, ...] = (
    "GATE1_NO_EXECUTION_PERMISSION",
    "GATE1_NO_TICKET",
    "GATE1_NO_RUNTIME_ENFORCEMENT",
    "GATE1_NO_HALT_PERSISTENCE",
    "GATE1_NO_RESET_APPLICATION",
    "PLANNED_CAP_DOES_NOT_GUARANTEE_REALISED_LOSS",
    "STOP_CONTRACT_DOES_NOT_PROVE_PROTECTION",
    "OPERATIONAL_SETTINGS_NOT_OWNER_APPROVED",
    "ALLOWLIST_EMPTY",
)


def has_reason(findings: Iterable[Finding], reason: Reason) -> bool:
    """Return whether any finding carries this reason."""
    return any(item.reason == reason for item in findings)


@dataclass(frozen=True, slots=True)
class Finding:
    """One failed check and the field it refers to."""

    reason: Reason
    detail: str


@dataclass(frozen=True, slots=True)
class ClockInput:
    """Caller-supplied clock. Gate 1 does not read the wall clock."""

    as_of: datetime | None
    previous_as_of: datetime | None = None


@dataclass(frozen=True, slots=True)
class QuoteInput:
    """Caller-supplied bid and ask. Not fetched, and not an execution quote."""

    bid: Decimal | None
    ask: Decimal | None
    observed_at: datetime | None
    source: str | None
    quality: str | None


@dataclass(frozen=True, slots=True)
class FxQuoteInput:
    """Caller-supplied FX. A missing rate is not defaulted."""

    rate_to_gbp: Decimal | None
    observed_at: datetime | None
    source: str | None


@dataclass(frozen=True, slots=True)
class SessionInput:
    """Caller-supplied calendar and session ids. No market calendar is built in."""

    calendar_id: str | None
    session_id: str | None
    in_session: bool | None


@dataclass(frozen=True, slots=True)
class FixtureOperationalLimits:
    """Test-only ages and source labels.

    ``owner_approved`` is fixed false. These numbers are not desk policy and
    the assessment still refuses execution permission.
    """

    owner_approved: Literal[False]
    label: Literal["NOT_OWNER_APPROVED_FIXTURE"]
    quote_max_age_seconds: int
    fx_max_age_seconds: int
    quote_sources: frozenset[str]
    fx_sources: frozenset[str]
    quality_labels: frozenset[str]
    calendar_id: str
    session_id: str

    def __post_init__(self) -> None:
        if self.owner_approved is not False or self.label != "NOT_OWNER_APPROVED_FIXTURE":
            raise RiskInputError(Reason.FIXTURE_NOT_OWNER_APPROVED, "fixture_limits")
        for name, value in (
            ("quote_max_age_seconds", self.quote_max_age_seconds),
            ("fx_max_age_seconds", self.fx_max_age_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RiskInputError(Reason.INVALID_DECIMAL, name)
        for name, values in (
            ("quote_sources", self.quote_sources),
            ("fx_sources", self.fx_sources),
            ("quality_labels", self.quality_labels),
        ):
            if not isinstance(values, frozenset) or any(not isinstance(item, str) or item.strip() == "" for item in values):
                raise RiskInputError(Reason.INVALID_DECIMAL, name)
        if self.calendar_id.strip() == "" or self.session_id.strip() == "":
            raise RiskInputError(Reason.SESSION_OR_CALENDAR_MISSING, "fixture_limits")


@dataclass(frozen=True, slots=True)
class AccountInput:
    """Point-in-time account inputs. Halt flags are supplied, not stored here."""

    currency: str
    live: bool
    simulated: bool
    marked_equity_gbp: Decimal
    equity_freshness: Freshness
    baseline_kind: BaselineKind
    opening_equity_gbp: Decimal | None
    opening_freshness: Freshness
    opening_london_date: date | None
    as_of_london_date: date
    net_external_deposits_gbp: Decimal
    available_cash_gbp: Decimal | None
    available_cash_status: KnownStatus
    gross_exposure_status: KnownStatus
    daily_halt_latched: bool
    loss_streak_halt_latched: bool


@dataclass(frozen=True, slots=True)
class BookPosition:
    """One supplied slice of a flat-to-flat episode.

    Pending and filled slices share an episode id when they are the same
    episode. Booked entry fees are recorded so the incremental-risk formula
    can leave them out; they are not added back.
    """

    episode_id: str
    instrument_id: str
    instrument_currency: str
    status: PositionStatus
    quantity: Decimal
    quantity_precision: int
    entry_price: Decimal | None
    mark_price: Decimal | None
    stop_price: Decimal | None
    stop_protection: StopProtection
    covered_quantity: Decimal | None
    fx_rate_to_gbp: Decimal | None
    conservative_price: Decimal | None
    conservative_fx_rate_to_gbp: Decimal | None
    unbooked_entry_cost_gbp: Decimal
    expected_exit_cost_gbp: Decimal
    adverse_allowance_gbp: Decimal
    booked_entry_fees_gbp: Decimal


@dataclass(frozen=True, slots=True)
class EntryProposal:
    """A long-only entry proposal. Aggregate assessment counts every item."""

    proposal_id: str
    episode_id: str
    instrument_id: str
    instrument_currency: str
    product: str
    side: str
    borrowing: bool
    leverage: Decimal
    opens_new_episode: bool
    quantity: Decimal
    quantity_precision: int
    entry_price: Decimal
    stop_price: Decimal | None
    stop_protection: StopProtection
    covered_quantity: Decimal | None
    fx_rate_to_gbp: Decimal
    conservative_price: Decimal
    conservative_fx_rate_to_gbp: Decimal
    expected_entry_cost_gbp: Decimal
    expected_exit_cost_gbp: Decimal
    adverse_allowance_gbp: Decimal


@dataclass(frozen=True, slots=True)
class ClosedEpisode:
    """A finished flat-to-flat episode net. A partial exit is not an episode."""

    episode_id: str
    origin_fill_id: str
    net_gbp: Decimal
    partial_exit: bool


@dataclass(frozen=True, slots=True)
class BudgetFigures:
    """Penny figures from one assessment. Presence is not permission to trade."""

    per_trade_cap_gbp: Decimal
    daily_threshold_gbp: Decimal
    day_loss_gbp: Decimal
    existing_incremental_risk_gbp: Decimal
    remaining_day_risk_gbp: Decimal
    aggregate_cap_gbp: Decimal
    aggregate_used_gbp: Decimal
    concentration_cap_gbp: Decimal
    proposal_planned_risk_gbp: tuple[Decimal, ...]
    proposal_notional_gbp: tuple[Decimal, ...]
    proposal_cash_required_gbp: tuple[Decimal, ...]
    instrument_notional_gbp: tuple[tuple[str, Decimal], ...]
    episode_count: int
    episode_cap: int
    available_cash_gbp: Decimal | None
    cash_required_gbp: Decimal
    consecutive_loss_episodes: int


@dataclass(frozen=True, slots=True)
class EntryAssessment:
    """Budget arithmetic and operational checks, kept apart.

    ``mathematical_budget_compliant`` can be true while execution permission
    is still false. Gate 1 never sets the permission fields true.
    """

    mathematical_budget_compliant: bool
    budget_findings: tuple[Finding, ...]
    operational_structure_ok: bool
    operational_findings: tuple[Finding, ...]
    operational_settings_approval: SettingsApproval
    operationally_eligible_for_execution: Literal[False]
    execution_permission_issued: Literal[False]
    ticket_issued: Literal[False]
    allowlist_permitted: Literal[False]
    runtime_enforced: Literal[False]
    supplied_daily_halt_blocks: bool
    supplied_loss_streak_halt_blocks: bool
    day_loss_known: bool
    point_in_time_day_loss_blocks: bool
    point_in_time_loss_streak: int | None
    point_in_time_loss_streak_blocks: bool
    planned_cap_guarantees_realised_loss: Literal[False]
    stop_contract_proves_protection: Literal[False]
    figures: BudgetFigures | None
    boundary: tuple[str, ...]

    def __post_init__(self) -> None:
        _reject_permission_bits(
            execution_permission_issued=self.execution_permission_issued,
            ticket_issued=self.ticket_issued,
            operationally_eligible_for_execution=self.operationally_eligible_for_execution,
            allowlist_permitted=self.allowlist_permitted,
            runtime_enforced=self.runtime_enforced,
            planned_cap_guarantees_realised_loss=self.planned_cap_guarantees_realised_loss,
            stop_contract_proves_protection=self.stop_contract_proves_protection,
        )
        if self.boundary != BOUNDARY_STATEMENTS:
            raise RiskInputError(Reason.TICKET_NOT_ISSUED, "boundary")


@dataclass(frozen=True, slots=True)
class ProtectiveExitProposal:
    """A reduce-only exit intent. Gate 1 does not submit it."""

    proposal_id: str
    episode_id: str
    instrument_id: str
    quantity: Decimal
    quantity_precision: int
    reduce_only: bool
    blind_liquidation: bool


@dataclass(frozen=True, slots=True)
class ProtectiveExitAssessment:
    """Structure check for a protective exit. Nothing is sent or liquidated."""

    structure_ok: bool
    findings: tuple[Finding, ...]
    entry_permission_issued: Literal[False]
    executed: Literal[False]
    blind_liquidation_performed: Literal[False]
    blocked_because_opening_baseline_missing: Literal[False]
    stop_contract_proves_protection: Literal[False]
    boundary: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            self.entry_permission_issued is not False
            or self.executed is not False
            or self.blind_liquidation_performed is not False
            or self.blocked_because_opening_baseline_missing is not False
            or self.stop_contract_proves_protection is not False
        ):
            raise RiskInputError(Reason.BLIND_LIQUIDATION_FORBIDDEN, "protective_exit")
        if self.boundary != BOUNDARY_STATEMENTS:
            raise RiskInputError(Reason.TICKET_NOT_ISSUED, "boundary")


@dataclass(frozen=True, slots=True)
class ResetRequest:
    """A request shape only. Validation does not apply or persist a reset."""

    actor: ResetActor
    reason: str
    halt_reasons: tuple[HaltReason, ...]
    trading_day_transition_confirmed: bool
    fresh_validated_account: bool
    owner_authorisation_claimed: bool


@dataclass(frozen=True, slots=True)
class ResetAuditRecord:
    """In-memory description of a reset decision. It is not written anywhere."""

    actor: str
    reason: str
    requested_halt_reasons: tuple[str, ...]
    decision: str
    applied: Literal[False]
    persisted: Literal[False]
    authorisation_is_proof: Literal[False]

    def __post_init__(self) -> None:
        if self.applied is not False or self.persisted is not False or self.authorisation_is_proof is not False:
            raise RiskInputError(Reason.RESET_ACTOR_NOT_OWNER, "audit")


@dataclass(frozen=True, slots=True)
class ResetValidation:
    """Structural reset check. ``applied`` is always false in Gate 1."""

    structurally_acceptable: bool
    findings: tuple[Finding, ...]
    applied: Literal[False]
    persisted: Literal[False]
    authorisation_is_proof: Literal[False]
    requested_halt_reasons: tuple[str, ...]
    cleared_halt_reasons: tuple[()]
    audit: ResetAuditRecord

    def __post_init__(self) -> None:
        if self.applied is not False or self.persisted is not False or self.authorisation_is_proof is not False:
            raise RiskInputError(Reason.RESET_ACTOR_NOT_OWNER, "reset")
        if self.cleared_halt_reasons != ():
            raise RiskInputError(Reason.RESET_NO_HALT_REASONS, "cleared_halt_reasons")


@dataclass(frozen=True, slots=True)
class TicketRefusal:
    """Gate 1 does not mint a simulation ticket or a signing secret."""

    issued: Literal[False]
    token: None
    execution_permission_issued: Literal[False]
    reason: Reason
    detail: str

    def __post_init__(self) -> None:
        if self.issued is not False or self.token is not None or self.execution_permission_issued is not False:
            raise RiskInputError(Reason.TICKET_NOT_ISSUED, "ticket")
        if self.reason != Reason.TICKET_NOT_ISSUED:
            raise RiskInputError(Reason.TICKET_NOT_ISSUED, "ticket.reason")


def _reject_permission_bits(
    *,
    execution_permission_issued: object,
    ticket_issued: object,
    operationally_eligible_for_execution: object,
    allowlist_permitted: object,
    runtime_enforced: object,
    planned_cap_guarantees_realised_loss: object,
    stop_contract_proves_protection: object,
) -> None:
    if (
        execution_permission_issued is not False
        or ticket_issued is not False
        or operationally_eligible_for_execution is not False
        or allowlist_permitted is not False
        or runtime_enforced is not False
        or planned_cap_guarantees_realised_loss is not False
        or stop_contract_proves_protection is not False
    ):
        raise RiskInputError(Reason.TICKET_NOT_ISSUED, "permission_bits")

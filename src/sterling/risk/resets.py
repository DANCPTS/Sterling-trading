"""Reset and ticket request contracts.

Validation records an in-memory decision. It does not persist an audit row,
clear a halt, check a credential, or mint a ticket. AI is never an authorised
reset actor. A complete owner request is still not an authorisation proof.
"""

from __future__ import annotations

from sterling.risk.contracts import (
    Finding,
    HaltReason,
    ResetActor,
    ResetAuditRecord,
    ResetRequest,
    ResetValidation,
    TicketRefusal,
)
from sterling.risk.reasons import Reason


def validate_reset_request(request: ResetRequest) -> ResetValidation:
    """Check reset shape only. Nothing is cleared or stored."""
    findings: list[tuple[Reason, str]] = []
    if not isinstance(request.actor, ResetActor):
        findings.append((Reason.RESET_ACTOR_NOT_OWNER, "actor"))
    elif request.actor == ResetActor.AI:
        findings.append((Reason.RESET_ACTOR_AI, "actor"))
    elif request.actor != ResetActor.OWNER:
        findings.append((Reason.RESET_ACTOR_NOT_OWNER, "actor"))

    reason = request.reason if isinstance(request.reason, str) else ""
    if reason.strip() == "":
        findings.append((Reason.RESET_REASON_MISSING, "reason"))

    named: list[str] = []
    halt_reasons = request.halt_reasons if isinstance(request.halt_reasons, tuple) else ()
    if len(halt_reasons) == 0:
        findings.append((Reason.RESET_NO_HALT_REASONS, "halt_reasons"))
    else:
        for item in halt_reasons:
            if not isinstance(item, HaltReason):
                findings.append((Reason.RESET_UNKNOWN_HALT_REASON, str(item)))
                continue
            if item.value in named:
                findings.append((Reason.RESET_UNKNOWN_HALT_REASON, item.value))
                continue
            named.append(item.value)

    if HaltReason.DAILY_LOSS in halt_reasons:
        if request.trading_day_transition_confirmed is not True:
            findings.append((Reason.RESET_DAILY_REQUIRES_TRADING_DAY_TRANSITION, "trading_day_transition_confirmed"))
        if request.fresh_validated_account is not True:
            findings.append((Reason.RESET_DAILY_REQUIRES_FRESH_ACCOUNT, "fresh_validated_account"))
        if request.owner_authorisation_claimed is not True:
            findings.append((Reason.RESET_REQUIRES_OWNER_AUTHORISATION, "daily_loss"))
    if HaltReason.LOSS_STREAK in halt_reasons:
        if request.owner_authorisation_claimed is not True:
            findings.append((Reason.RESET_REQUIRES_OWNER_AUTHORISATION, "loss_streak"))

    acceptable = not findings
    audit = ResetAuditRecord(
        actor=request.actor.value if isinstance(request.actor, ResetActor) else str(request.actor),
        reason=reason,
        requested_halt_reasons=tuple(named),
        decision="NOT_APPLIED_GATE1" if acceptable else "REJECTED",
        applied=False,
        persisted=False,
        authorisation_is_proof=False,
    )
    return ResetValidation(
        structurally_acceptable=acceptable,
        findings=tuple(Finding(reason_code, detail) for reason_code, detail in findings),
        applied=False,
        persisted=False,
        authorisation_is_proof=False,
        requested_halt_reasons=tuple(named),
        cleared_halt_reasons=(),
        audit=audit,
    )


def refuse_simulation_ticket(proposal_id: str) -> TicketRefusal:
    """Refuse to issue a ticket. No token and no signing secret are created."""
    detail = "GATE1_DOES_NOT_ISSUE_TICKETS"
    if not isinstance(proposal_id, str) or proposal_id.strip() == "":
        detail = "GATE1_DOES_NOT_ISSUE_TICKETS; proposal_id missing"
    return TicketRefusal(
        issued=False,
        token=None,
        execution_permission_issued=False,
        reason=Reason.TICKET_NOT_ISSUED,
        detail=detail,
    )

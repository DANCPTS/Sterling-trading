"""Owner-policy pins. Fixture numbers are not accepted as approval."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from sterling.placeholders.risk import RiskEngine
from sterling.risk.approved import POLICY_CONTENT_SHA256, STAGE1_APPROVED_CONTENT_SHA256
from sterling.risk.contracts import FixtureOperationalLimits, HaltReason, ResetActor, ResetRequest, ResetValidation
from sterling.risk.errors import PolicyError, RiskInputError
from sterling.risk.policy import default_policy_text, load_default_policy, loads_policy
from sterling.risk.reasons import Reason
from sterling.risk.resets import refuse_simulation_ticket, validate_reset_request
from sterling.rules.approved import APPROVED_CONTENT_SHA256
from sterling.rules.hashing import content_hash
from sterling.rules.loader import load_default_rules
from sterling.rules.model import instrument_allowed


def _document() -> dict[str, object]:
    loaded = json.loads(default_policy_text())
    assert isinstance(loaded, dict)
    return loaded


def _rehashed(document: dict[str, object]) -> str:
    encoded = json.dumps(document)
    parsed = json.loads(encoded)
    assert isinstance(parsed, dict)
    parsed["content_hash"] = content_hash(parsed)
    return json.dumps(parsed)


def test_default_policy_cites_the_unchanged_stage1_pin() -> None:
    policy = load_default_policy()
    rules = load_default_rules()
    assert STAGE1_APPROVED_CONTENT_SHA256 == APPROVED_CONTENT_SHA256
    assert STAGE1_APPROVED_CONTENT_SHA256 == "d28912d913d9b8d066dca38811c207f921764294dba72c5392aa49523073279c"
    assert policy.stage1_content_hash == APPROVED_CONTENT_SHA256
    assert policy.stage1_rules_version_id == "desk-rules-v0.2"
    assert policy.content_hash == POLICY_CONTENT_SHA256
    assert content_hash(_document()) == POLICY_CONTENT_SHA256
    assert rules.content_hash == APPROVED_CONTENT_SHA256
    assert policy.starting_capital_gbp == Decimal("500.00")
    assert policy.per_trade_absolute_ceiling_gbp == Decimal("2.50")
    assert policy.daily_loss_absolute_ceiling_gbp == Decimal("5.00")
    assert policy.concentration_equity_fraction == Decimal("0.25")
    assert policy.max_simultaneous_position_episodes == 4
    assert policy.instrument_allowlist == ()
    assert instrument_allowed(rules, "SYNTH-EQ") is False
    assert policy.operational_settings_state == "UNAPPROVED_UNCONFIGURED"
    assert policy.default_operational_decision == "DENY"
    assert policy.fixture_policy_usable_as_owner_policy is False
    assert policy.execution_permission == "NOT_ISSUED"
    assert policy.runtime_enforcement is False
    assert policy.halt_persistence is False
    assert policy.reset_application is False
    assert policy.ticket_issuance is False
    assert policy.atomic_stop_execution is False
    assert policy.os_isolation_verified is False
    assert policy.simulator_execution is False
    assert policy.planned_cap_guarantees_realised_loss is False
    assert policy.stop_contract_proves_protection is False
    assert not hasattr(policy, "quote_max_age_seconds")


def test_rewritten_hash_does_not_approve_a_new_cap() -> None:
    raised = _document()
    assert isinstance(raised["budgets"], dict)
    raised["budgets"]["concentration_equity_fraction"] = "0.50"
    with pytest.raises(PolicyError, match="caps do not match"):
        loads_policy(_rehashed(raised))
    episodes = _document()
    assert isinstance(episodes["budgets"], dict)
    episodes["budgets"]["max_simultaneous_position_episodes"] = 5
    with pytest.raises(PolicyError, match="caps do not match"):
        loads_policy(_rehashed(episodes))


def test_allowlist_live_and_quote_age_cannot_be_slipped_into_the_policy() -> None:
    allowlist = _document()
    assert isinstance(allowlist["orders"], dict)
    allowlist["orders"]["instrument_allowlist"] = ["SYNTH-EQ"]
    with pytest.raises(PolicyError, match="allowlist"):
        loads_policy(_rehashed(allowlist))
    live = _document()
    assert isinstance(live["account"], dict)
    live["account"]["live"] = True
    with pytest.raises(PolicyError, match="live"):
        loads_policy(_rehashed(live))
    aged = _document()
    assert isinstance(aged["operational_numerical_settings"], dict)
    aged["operational_numerical_settings"]["quote_max_age"] = 30
    with pytest.raises(PolicyError, match="UNAPPROVED_UNCONFIGURED"):
        loads_policy(_rehashed(aged))
    floated = default_policy_text().replace('"0.25"', "0.25", 1)
    with pytest.raises(PolicyError, match="not floats"):
        loads_policy(floated)


def test_stage1_risk_placeholder_still_does_not_enforce() -> None:
    with pytest.raises(NotImplementedError, match="not a halt"):
        RiskEngine().evaluate()


def test_fixture_limits_cannot_claim_owner_approval() -> None:
    with pytest.raises(RiskInputError):
        FixtureOperationalLimits(
            owner_approved=True,  # type: ignore[arg-type]
            label="NOT_OWNER_APPROVED_FIXTURE",
            quote_max_age_seconds=5,
            fx_max_age_seconds=5,
            quote_sources=frozenset({"FIXTURE-SOURCE"}),
            fx_sources=frozenset({"FIXTURE-FX"}),
            quality_labels=frozenset({"FIXTURE-QUALITY"}),
            calendar_id="FIXTURE-CAL",
            session_id="FIXTURE-SESSION",
        )


def test_reset_contract_rejects_ai_and_a_missing_reason_without_applying() -> None:
    ai = validate_reset_request(
        ResetRequest(
            actor=ResetActor.AI,
            reason="model asked to resume",
            halt_reasons=(HaltReason.DAILY_LOSS, HaltReason.LOSS_STREAK),
            trading_day_transition_confirmed=True,
            fresh_validated_account=True,
            owner_authorisation_claimed=True,
        )
    )
    assert has(ai, Reason.RESET_ACTOR_AI)
    assert ai.structurally_acceptable is False
    assert ai.applied is False
    assert ai.persisted is False
    assert ai.authorisation_is_proof is False
    assert ai.cleared_halt_reasons == ()
    assert ai.audit.decision == "REJECTED"

    missing = validate_reset_request(
        ResetRequest(
            actor=ResetActor.OWNER,
            reason="  ",
            halt_reasons=(HaltReason.DAILY_LOSS,),
            trading_day_transition_confirmed=True,
            fresh_validated_account=True,
            owner_authorisation_claimed=True,
        )
    )
    assert has(missing, Reason.RESET_REASON_MISSING)
    assert missing.applied is False
    assert missing.cleared_halt_reasons == ()

    incomplete_day = validate_reset_request(
        ResetRequest(
            actor=ResetActor.OWNER,
            reason="attempted daily clear",
            halt_reasons=(HaltReason.DAILY_LOSS,),
            trading_day_transition_confirmed=False,
            fresh_validated_account=True,
            owner_authorisation_claimed=True,
        )
    )
    assert has(incomplete_day, Reason.RESET_DAILY_REQUIRES_TRADING_DAY_TRANSITION)
    assert incomplete_day.cleared_halt_reasons == ()

    streak_only = validate_reset_request(
        ResetRequest(
            actor=ResetActor.OWNER,
            reason="owner reviewed the three lost episodes",
            halt_reasons=(HaltReason.LOSS_STREAK,),
            trading_day_transition_confirmed=False,
            fresh_validated_account=False,
            owner_authorisation_claimed=True,
        )
    )
    assert streak_only.structurally_acceptable is True
    assert streak_only.applied is False
    assert streak_only.authorisation_is_proof is False
    assert streak_only.requested_halt_reasons == ("LOSS_STREAK",)
    assert streak_only.cleared_halt_reasons == ()
    assert "DAILY_LOSS" not in streak_only.requested_halt_reasons

    shaped = validate_reset_request(
        ResetRequest(
            actor=ResetActor.OWNER,
            reason="new London trading day and a fresh validated account",
            halt_reasons=(HaltReason.DAILY_LOSS,),
            trading_day_transition_confirmed=True,
            fresh_validated_account=True,
            owner_authorisation_claimed=True,
        )
    )
    assert shaped.structurally_acceptable is True
    assert shaped.applied is False
    assert shaped.persisted is False
    assert shaped.authorisation_is_proof is False
    assert shaped.cleared_halt_reasons == ()
    assert shaped.audit.applied is False
    assert shaped.audit.decision == "NOT_APPLIED_GATE1"


def test_reset_shape_rejects_non_owner_actors_and_incomplete_daily_requests() -> None:
    system = validate_reset_request(
        ResetRequest(
            actor=ResetActor.SYSTEM,
            reason="automatic",
            halt_reasons=(HaltReason.LOSS_STREAK,),
            trading_day_transition_confirmed=False,
            fresh_validated_account=False,
            owner_authorisation_claimed=True,
        )
    )
    assert has(system, Reason.RESET_ACTOR_NOT_OWNER)
    assert system.applied is False
    empty = validate_reset_request(
        ResetRequest(
            actor=ResetActor.OWNER,
            reason="no target",
            halt_reasons=(),
            trading_day_transition_confirmed=True,
            fresh_validated_account=True,
            owner_authorisation_claimed=True,
        )
    )
    assert has(empty, Reason.RESET_NO_HALT_REASONS)
    stale_account = validate_reset_request(
        ResetRequest(
            actor=ResetActor.OWNER,
            reason="day changed",
            halt_reasons=(HaltReason.DAILY_LOSS,),
            trading_day_transition_confirmed=True,
            fresh_validated_account=False,
            owner_authorisation_claimed=True,
        )
    )
    assert has(stale_account, Reason.RESET_DAILY_REQUIRES_FRESH_ACCOUNT)
    unauthorised = validate_reset_request(
        ResetRequest(
            actor=ResetActor.OWNER,
            reason="streak",
            halt_reasons=(HaltReason.LOSS_STREAK,),
            trading_day_transition_confirmed=False,
            fresh_validated_account=False,
            owner_authorisation_claimed=False,
        )
    )
    assert has(unauthorised, Reason.RESET_REQUIRES_OWNER_AUTHORISATION)
    duplicate = validate_reset_request(
        ResetRequest(
            actor=ResetActor.OWNER,
            reason="twice",
            halt_reasons=(HaltReason.DAILY_LOSS, HaltReason.DAILY_LOSS),
            trading_day_transition_confirmed=True,
            fresh_validated_account=True,
            owner_authorisation_claimed=True,
        )
    )
    assert has(duplicate, Reason.RESET_UNKNOWN_HALT_REASON)
    assert duplicate.cleared_halt_reasons == ()


def test_ticket_request_is_refused() -> None:
    refusal = refuse_simulation_ticket("P1")
    assert refusal.issued is False
    assert refusal.token is None
    assert refusal.execution_permission_issued is False
    assert refusal.reason is Reason.TICKET_NOT_ISSUED
    assert "secret" not in refusal.detail
    missing = refuse_simulation_ticket("  ")
    assert missing.issued is False
    assert missing.token is None
    assert "missing" in missing.detail


def has(result: ResetValidation, reason: Reason) -> bool:
    return any(item.reason == reason for item in result.findings)

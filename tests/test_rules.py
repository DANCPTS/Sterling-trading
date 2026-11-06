"""Rules loading, hash pin, live rejection, and ceiling rejection."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from sterling.errors import (
    CeilingRaised,
    LiveTradingRejected,
    RulesHashMismatch,
    RulesValidationError,
    UnapprovedRulesVersion,
)
from sterling.rules.approved import APPROVED_CONTENT_SHA256, APPROVED_RULES_VERSION_ID
from sterling.rules.hashing import content_hash
from sterling.rules.loader import (
    default_rules_text,
    load_default_rules,
    loads_rules,
    validate_rules_document,
)
from sterling.rules.model import instrument_allowed


def _document() -> dict[str, object]:
    loaded = json.loads(default_rules_text())
    assert isinstance(loaded, dict)
    return loaded


def _rehashed(document: dict[str, object]) -> dict[str, object]:
    document["content_hash"] = content_hash(document)
    return document


def test_default_rules_match_the_owner_pin() -> None:
    rules = load_default_rules()
    document = _document()
    assert rules.version == "0.2"
    assert rules.version_id == APPROVED_RULES_VERSION_ID
    assert rules.content_hash == APPROVED_CONTENT_SHA256
    assert content_hash(document) == APPROVED_CONTENT_SHA256
    assert rules.starting_capital == Decimal("500.00")
    assert rules.currency == "GBP"
    assert rules.live is False
    assert rules.live_trading is False
    assert rules.simulated is True
    assert rules.instrument_allowlist == ()
    assert rules.per_trade_equity_fraction == Decimal("0.005")
    assert rules.per_trade_absolute_ceiling == Decimal("2.50")
    assert rules.per_trade_includes_expected_costs is True
    assert rules.per_trade_includes_adverse_execution_allowance is True
    assert rules.daily_loss_equity_fraction == Decimal("0.01")
    assert rules.daily_loss_absolute_ceiling == Decimal("5.00")
    assert rules.daily_loss_behaviour == "reactive_halt"
    assert rules.daily_loss_guaranteed_cap is False
    assert rules.total_open_planned_risk_equity_fraction == Decimal("0.01")
    assert rules.consecutive_loss_halt == 3
    assert rules.halt_restart == "human_only"
    assert rules.long_only is True
    assert rules.unleveraged is True
    assert rules.borrowing_allowed is False
    assert rules.shorts_allowed is False
    assert rules.cfds_allowed is False
    assert rules.futures_allowed is False
    assert rules.options_allowed is False
    assert rules.leveraged_or_inverse_allowed is False
    assert rules.holding_period_min_days == 2
    assert rules.holding_period_max_days == 10
    assert rules.holding_period_provisional is True
    assert rules.regular_session_only is True
    assert rules.day_boundary_timezone == "Europe/London"
    assert rules.day_boundary_local_time == "00:00"
    assert instrument_allowed(rules, "SYNTH-EQ") is False
    assert instrument_allowed(rules, "VOD.L") is False


def test_live_flag_is_rejected_even_with_a_rewritten_hash() -> None:
    account = _rehashed(_with_live("account"))
    orders = _rehashed(_with_live("orders"))
    with pytest.raises(LiveTradingRejected):
        validate_rules_document(account)
    with pytest.raises(LiveTradingRejected):
        validate_rules_document(orders)


def test_raised_ceilings_are_rejected_even_with_a_rewritten_hash() -> None:
    per_trade = _document()
    per_trade["risk"]["per_trade_planned_risk"]["absolute_ceiling"] = "10.00"  # type: ignore[index]
    with pytest.raises(CeilingRaised):
        validate_rules_document(_rehashed(per_trade))

    daily = _document()
    daily["risk"]["daily_loss"]["absolute_ceiling"] = "5.01"  # type: ignore[index]
    with pytest.raises(CeilingRaised):
        validate_rules_document(_rehashed(daily))


def test_lowered_ceiling_does_not_match_v0_2() -> None:
    document = _document()
    document["risk"]["per_trade_planned_risk"]["absolute_ceiling"] = "2.49"  # type: ignore[index]
    with pytest.raises(RulesValidationError):
        validate_rules_document(_rehashed(document))


def test_bumping_the_version_does_not_approve_a_new_file() -> None:
    document = _document()
    document["version"] = "0.3"
    document["version_id"] = "desk-rules-v0.3"
    with pytest.raises(UnapprovedRulesVersion):
        validate_rules_document(_rehashed(document))


def test_allowlist_must_stay_empty() -> None:
    document = _document()
    document["orders"]["instrument_allowlist"] = ["SYNTH-EQ"]  # type: ignore[index]
    with pytest.raises(RulesValidationError, match="allowlist"):
        validate_rules_document(_rehashed(document))


def test_shorts_cannot_be_enabled() -> None:
    document = _document()
    document["products"]["shorts"] = True  # type: ignore[index]
    with pytest.raises(RulesValidationError):
        validate_rules_document(_rehashed(document))


def test_stale_content_hash_is_rejected() -> None:
    document = _document()
    document["description"] = "tampered description"
    with pytest.raises(RulesHashMismatch, match="does not match the canonical"):
        validate_rules_document(document)


def test_self_consistent_unapproved_body_is_rejected() -> None:
    document = _document()
    document["description"] = "tampered description"
    with pytest.raises(RulesHashMismatch, match="not the owner-approved"):
        validate_rules_document(_rehashed(document))


def test_float_in_the_rules_file_is_rejected() -> None:
    text = default_rules_text().replace('"500.00"', "500.00", 1)
    with pytest.raises(RulesValidationError):
        loads_rules(text)


def test_missing_key_is_rejected() -> None:
    document = _document()
    del document["description"]
    with pytest.raises(RulesValidationError, match="description"):
        validate_rules_document(document)


def _with_live(section: str) -> dict[str, object]:
    document = _document()
    if section == "account":
        document["account"]["live"] = True  # type: ignore[index]
    else:
        document["orders"]["live_trading"] = True  # type: ignore[index]
    return document

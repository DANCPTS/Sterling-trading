"""Aggregate Gate 1 assessments. No fills are executed."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from sterling.risk.contracts import (
    BaselineKind,
    ClockInput,
    ClosedEpisode,
    EntryAssessment,
    Freshness,
    FxQuoteInput,
    KnownStatus,
    PositionStatus,
    ProtectiveExitProposal,
    QuoteInput,
    SessionInput,
    SettingsApproval,
    StopProtection,
    has_reason,
)
from sterling.risk.errors import RiskInputError
from sterling.risk.policy import Stage2Policy, load_default_policy
from sterling.risk.reasons import Reason
from sterling.risk.validation import assess_long_entries, assess_protective_exit
from tests.risk_support import (
    AS_OF,
    account,
    fixture_limits,
    flat_slice,
    fresh_fx,
    fresh_quote,
    fresh_session,
    open_slice,
    policy_clock,
    proposal,
)


@pytest.fixture(scope="module")
def policy() -> Stage2Policy:
    return load_default_policy()


def _assess(policy: Stage2Policy, **overrides: object) -> EntryAssessment:
    values: dict[str, object] = {
        "policy": policy,
        "account": account(),
        "clock": policy_clock(),
        "proposals": (proposal(),),
    }
    values.update(overrides)
    return assess_long_entries(**values)  # type: ignore[arg-type]


def test_five_hundred_pound_anchor_is_budget_compliant_and_not_permission(policy: Stage2Policy) -> None:
    result = _assess(policy)
    assert result.mathematical_budget_compliant is True
    assert result.budget_findings == ()
    assert result.operational_structure_ok is False
    assert result.operational_settings_approval is SettingsApproval.UNAPPROVED_UNCONFIGURED
    assert result.operationally_eligible_for_execution is False
    assert result.execution_permission_issued is False
    assert result.ticket_issued is False
    assert result.allowlist_permitted is False
    assert result.runtime_enforced is False
    assert result.planned_cap_guarantees_realised_loss is False
    assert result.stop_contract_proves_protection is False
    assert has_reason(result.operational_findings, Reason.ALLOWLIST_EMPTY)
    assert has_reason(result.operational_findings, Reason.SETTINGS_UNAPPROVED_UNCONFIGURED)
    assert has_reason(result.operational_findings, Reason.QUOTE_MISSING)
    figures = result.figures
    assert figures is not None
    assert figures.per_trade_cap_gbp == Decimal("2.50")
    assert figures.daily_threshold_gbp == Decimal("5.00")
    assert figures.day_loss_gbp == Decimal("0.00")
    assert figures.remaining_day_risk_gbp == Decimal("5.00")
    assert figures.aggregate_cap_gbp == Decimal("5.00")
    assert figures.concentration_cap_gbp == Decimal("125.00")
    assert figures.proposal_planned_risk_gbp == (Decimal("2.50"),)
    assert figures.proposal_notional_gbp == (Decimal("10.00"),)
    assert figures.episode_count == 1
    assert figures.episode_cap == 4
    assert "SYNTH-EQ" not in policy.instrument_allowlist


def test_equality_is_allowed_and_one_penny_over_is_not(policy: Stage2Policy) -> None:
    exact = _assess(
        policy,
        proposals=(
            proposal(quantity=Decimal("12.5"), quantity_precision=1, covered_quantity=Decimal("12.5"), stop_price=Decimal("9.80")),
        ),
    )
    assert exact.mathematical_budget_compliant is True
    assert exact.figures is not None
    assert exact.figures.proposal_planned_risk_gbp == (Decimal("2.50"),)
    assert exact.figures.proposal_notional_gbp == (Decimal("125.00"),)
    over = _assess(policy, proposals=(proposal(stop_price=Decimal("7.49")),))
    assert over.mathematical_budget_compliant is False
    assert has_reason(over.budget_findings, Reason.PER_TRADE_CAP_EXCEEDED)
    assert over.execution_permission_issued is False


def test_rounding_edge_is_up_for_risk(policy: Stage2Policy) -> None:
    allowed = _assess(policy, proposals=(proposal(stop_price=Decimal("7.501")),))
    denied = _assess(policy, proposals=(proposal(stop_price=Decimal("7.499")),))
    assert allowed.figures is not None and allowed.figures.proposal_planned_risk_gbp == (Decimal("2.50"),)
    assert allowed.mathematical_budget_compliant is True
    assert denied.figures is not None and denied.figures.proposal_planned_risk_gbp == (Decimal("2.51"),)
    assert denied.mathematical_budget_compliant is False


def test_fractional_quantity_uses_declared_precision(policy: Stage2Policy) -> None:
    result = _assess(
        policy,
        proposals=(
            proposal(
                quantity=Decimal("0.33333333"),
                quantity_precision=8,
                covered_quantity=Decimal("0.33333333"),
                stop_price=Decimal("9"),
            ),
        ),
    )
    assert result.figures is not None
    assert result.figures.proposal_planned_risk_gbp == (Decimal("0.34"),)
    assert result.mathematical_budget_compliant is True
    too_fine = _assess(
        policy,
        proposals=(proposal(quantity=Decimal("0.33333333"), quantity_precision=2, covered_quantity=Decimal("0.33333333")),),
    )
    assert has_reason(too_fine.budget_findings, Reason.SCALE_EXCEEDED)


def test_lower_equity_scales_and_large_equity_keeps_absolute_caps(policy: Stage2Policy) -> None:
    small = _assess(
        policy,
        account=account(
            marked_equity_gbp=Decimal("100.00"), opening_equity_gbp=Decimal("100.00"), available_cash_gbp=Decimal("100.00")
        ),
        proposals=(proposal(stop_price=Decimal("9.50")),),
    )
    assert small.figures is not None
    assert small.figures.per_trade_cap_gbp == Decimal("0.50")
    assert small.figures.daily_threshold_gbp == Decimal("1.00")
    assert small.figures.concentration_cap_gbp == Decimal("25.00")
    assert small.mathematical_budget_compliant is True
    large = _assess(
        policy,
        account=account(
            marked_equity_gbp=Decimal("100000.00"),
            opening_equity_gbp=Decimal("100000.00"),
            available_cash_gbp=Decimal("100000.00"),
        ),
    )
    assert large.figures is not None
    assert large.figures.per_trade_cap_gbp == Decimal("2.50")
    assert large.figures.daily_threshold_gbp == Decimal("5.00")
    assert large.figures.aggregate_cap_gbp == Decimal("1000.00")
    assert large.figures.concentration_cap_gbp == Decimal("25000.00")


def test_daily_remaining_and_threshold_equality(policy: Stage2Policy) -> None:
    remaining = _assess(
        policy,
        account=account(marked_equity_gbp=Decimal("496.00")),
        proposals=(proposal(stop_price=Decimal("8.50")),),
    )
    assert remaining.figures is not None
    assert remaining.figures.day_loss_gbp == Decimal("4.00")
    assert remaining.figures.remaining_day_risk_gbp == Decimal("1.00")
    assert remaining.figures.proposal_planned_risk_gbp == (Decimal("1.50"),)
    assert has_reason(remaining.budget_findings, Reason.DAILY_REMAINING_EXCEEDED)
    assert remaining.point_in_time_day_loss_blocks is False
    blocked = _assess(
        policy,
        account=account(marked_equity_gbp=Decimal("495.00")),
        proposals=(proposal(stop_price=Decimal("9.90")),),
    )
    assert blocked.figures is not None
    assert blocked.figures.day_loss_gbp == Decimal("5.00")
    assert blocked.point_in_time_day_loss_blocks is True
    assert has_reason(blocked.budget_findings, Reason.DAY_LOSS_AT_OR_ABOVE_THRESHOLD)
    assert blocked.mathematical_budget_compliant is False


def test_external_flows_do_not_reset_the_daily_budget(policy: Stage2Policy) -> None:
    deposited = _assess(
        policy,
        account=account(marked_equity_gbp=Decimal("600.00"), net_external_deposits_gbp=Decimal("100.00")),
    )
    assert deposited.figures is not None
    assert deposited.figures.day_loss_gbp == Decimal("0.00")
    assert deposited.figures.daily_threshold_gbp == Decimal("5.00")
    assert deposited.figures.per_trade_cap_gbp == Decimal("2.50")
    withdrawn = _assess(
        policy,
        account=account(
            marked_equity_gbp=Decimal("490.00"),
            net_external_deposits_gbp=Decimal("-10.00"),
            available_cash_gbp=Decimal("490.00"),
        ),
    )
    assert withdrawn.figures is not None
    assert withdrawn.figures.day_loss_gbp == Decimal("0.00")


def test_booked_fees_are_not_added_to_incremental_risk(policy: Stage2Policy) -> None:
    held = open_slice(
        "E-open",
        entry="12.00",
        mark="10.00",
        stop="9.00",
        exit_cost="0.10",
        adverse="0.05",
        booked_fee="0.40",
    )
    added = proposal(
        proposal_id="P-add",
        episode_id="E-new",
        stop_price=Decimal("8.55"),
        opens_new_episode=True,
    )
    result = _assess(
        policy,
        account=account(marked_equity_gbp=Decimal("497.60"), available_cash_gbp=Decimal("400.00")),
        positions=(held,),
        proposals=(added,),
    )
    assert result.figures is not None
    assert result.figures.existing_incremental_risk_gbp == Decimal("1.15")
    assert result.figures.day_loss_gbp == Decimal("2.40")
    assert result.figures.remaining_day_risk_gbp == Decimal("1.45")
    assert result.figures.proposal_planned_risk_gbp == (Decimal("1.45"),)
    assert result.mathematical_budget_compliant is True


def test_aggregate_reservations_share_one_budget(policy: Stage2Policy) -> None:
    first = proposal(proposal_id="P1", episode_id="E1", stop_price=Decimal("7.50"))
    second = proposal(proposal_id="P2", episode_id="E2", stop_price=Decimal("7.50"))
    both = _assess(policy, proposals=(first, second))
    assert both.figures is not None
    assert both.figures.aggregate_used_gbp == Decimal("5.00")
    assert both.mathematical_budget_compliant is True
    third = proposal(proposal_id="P3", episode_id="E3", stop_price=Decimal("9.00"))
    crowded = _assess(policy, proposals=(first, second, third))
    assert has_reason(crowded.budget_findings, Reason.AGGREGATE_RISK_EXCEEDED)
    assert crowded.mathematical_budget_compliant is False


def test_concentration_adds_current_pending_and_new_notional(policy: Stage2Policy) -> None:
    current = open_slice("E-open", quantity="10", precision=0, entry="5.00", mark="5.00", stop="4.90", conservative="5.00")
    pending = open_slice(
        "E-pend",
        status=PositionStatus.PENDING_OPEN,
        quantity="4",
        entry="10.00",
        stop="9.90",
        conservative="10.00",
    )
    over = proposal(
        episode_id="E-new",
        quantity=Decimal("3"),
        quantity_precision=0,
        covered_quantity=Decimal("3"),
        entry_price=Decimal("12.00"),
        stop_price=Decimal("11.90"),
        conservative_price=Decimal("12.00"),
    )
    denied = _assess(policy, positions=(current, pending), proposals=(over,))
    assert denied.figures is not None
    assert dict(denied.figures.instrument_notional_gbp)["SYNTH-EQ"] == Decimal("126.00")
    assert has_reason(denied.budget_findings, Reason.CONCENTRATION_EXCEEDED)
    assert not has_reason(denied.budget_findings, Reason.PER_TRADE_CAP_EXCEEDED)
    fitted = proposal(
        episode_id="E-new",
        quantity=Decimal("3"),
        quantity_precision=0,
        covered_quantity=Decimal("3"),
        entry_price=Decimal("11.00"),
        stop_price=Decimal("10.90"),
        conservative_price=Decimal("11.00"),
    )
    allowed = _assess(policy, positions=(current, pending), proposals=(fitted,))
    assert allowed.figures is not None
    assert dict(allowed.figures.instrument_notional_gbp)["SYNTH-EQ"] == Decimal("123.00")
    assert allowed.mathematical_budget_compliant is True
    assert allowed.figures.episode_count == 3


def test_wide_stop_fails_risk_while_notional_fits(policy: Stage2Policy) -> None:
    result = _assess(policy, proposals=(proposal(stop_price=Decimal("5.00")),))
    assert result.figures is not None
    assert result.figures.proposal_planned_risk_gbp == (Decimal("5.00"),)
    assert result.figures.proposal_notional_gbp == (Decimal("10.00"),)
    assert has_reason(result.budget_findings, Reason.PER_TRADE_CAP_EXCEEDED)
    assert not has_reason(result.budget_findings, Reason.CONCENTRATION_EXCEEDED)


def test_cash_is_not_stop_risk_and_unknown_denies(policy: Stage2Policy) -> None:
    tight = proposal(
        quantity=Decimal("10"),
        quantity_precision=0,
        covered_quantity=Decimal("10"),
        entry_price=Decimal("2.00"),
        stop_price=Decimal("1.99"),
        conservative_price=Decimal("2.00"),
        expected_entry_cost_gbp=Decimal("0.01"),
    )
    denied = _assess(policy, account=account(available_cash_gbp=Decimal("20.00")), proposals=(tight,))
    assert denied.figures is not None
    assert denied.figures.proposal_planned_risk_gbp == (Decimal("0.11"),)
    assert denied.figures.cash_required_gbp == Decimal("20.01")
    assert has_reason(denied.budget_findings, Reason.CASH_EXCEEDED)
    assert not has_reason(denied.budget_findings, Reason.PER_TRADE_CAP_EXCEEDED)
    exact = _assess(
        policy,
        account=account(available_cash_gbp=Decimal("20.00")),
        proposals=(
            proposal(
                quantity=Decimal("10"),
                quantity_precision=0,
                covered_quantity=Decimal("10"),
                entry_price=Decimal("2.00"),
                stop_price=Decimal("1.99"),
                conservative_price=Decimal("2.00"),
            ),
        ),
    )
    assert exact.mathematical_budget_compliant is True
    unknown_cash = _assess(
        policy,
        account=account(available_cash_status=KnownStatus.UNKNOWN, available_cash_gbp=None),
        proposals=(proposal(stop_price=Decimal("9.90")),),
    )
    assert has_reason(unknown_cash.budget_findings, Reason.CASH_UNKNOWN)
    assert unknown_cash.mathematical_budget_compliant is False
    unknown_exposure = _assess(
        policy,
        account=account(gross_exposure_status=KnownStatus.UNKNOWN),
        proposals=(proposal(stop_price=Decimal("9.90")),),
    )
    assert has_reason(unknown_exposure.budget_findings, Reason.EXPOSURE_UNKNOWN)
    assert unknown_exposure.mathematical_budget_compliant is False


def test_episode_count_includes_pending_and_rejects_the_fifth(policy: Stage2Policy) -> None:
    held = tuple(open_slice(f"E{index}") for index in range(1, 5))
    fifth = _assess(policy, positions=held, proposals=(proposal(episode_id="E5", stop_price=Decimal("9.90")),))
    assert fifth.figures is not None
    assert fifth.figures.episode_count == 5
    assert has_reason(fifth.budget_findings, Reason.EPISODE_CAP_EXCEEDED)
    added = proposal(episode_id="E1", opens_new_episode=False, stop_price=Decimal("9.90"))
    fourth_add = _assess(policy, positions=held, proposals=(added,))
    assert fourth_add.figures is not None
    assert fourth_add.figures.episode_count == 4
    assert fourth_add.mathematical_budget_compliant is True
    three = tuple(open_slice(f"E{index}") for index in range(1, 4))
    pair = (
        proposal(proposal_id="P1", episode_id="E8", stop_price=Decimal("9.90")),
        proposal(proposal_id="P2", episode_id="E9", stop_price=Decimal("9.90")),
    )
    concurrent = _assess(policy, positions=three, proposals=pair)
    assert concurrent.figures is not None
    assert concurrent.figures.episode_count == 5
    assert has_reason(concurrent.budget_findings, Reason.EPISODE_CAP_EXCEEDED)


def test_reentry_is_distinct_and_a_partial_is_not(policy: Stage2Policy) -> None:
    reentry = _assess(
        policy,
        positions=(flat_slice("E-old"),),
        proposals=(proposal(episode_id="E-new", stop_price=Decimal("9.90")),),
    )
    assert reentry.figures is not None
    assert reentry.figures.episode_count == 1
    assert reentry.mathematical_budget_compliant is True
    reused = _assess(
        policy,
        positions=(flat_slice("E-old"),),
        proposals=(proposal(episode_id="E-old", stop_price=Decimal("9.90")),),
    )
    assert has_reason(reused.budget_findings, Reason.EPISODE_ID_REUSED)
    partial = _assess(
        policy,
        positions=(open_slice("E1"),),
        proposals=(proposal(episode_id="E1", opens_new_episode=False, stop_price=Decimal("9.90")),),
    )
    assert partial.figures is not None
    assert partial.figures.episode_count == 1
    assert partial.mathematical_budget_compliant is True


def test_concurrent_cash_cannot_be_spent_twice(policy: Stage2Policy) -> None:
    buy = dict(
        quantity=Decimal("30"),
        quantity_precision=0,
        covered_quantity=Decimal("30"),
        entry_price=Decimal("2.00"),
        stop_price=Decimal("1.99"),
        conservative_price=Decimal("2.00"),
    )
    both = _assess(
        policy,
        account=account(available_cash_gbp=Decimal("100.00")),
        proposals=(
            proposal(proposal_id="P1", episode_id="E1", **buy),
            proposal(proposal_id="P2", episode_id="E2", **buy),
        ),
    )
    assert both.figures is not None
    assert both.figures.cash_required_gbp == Decimal("120.00")
    assert has_reason(both.budget_findings, Reason.CASH_EXCEEDED)


def test_invalid_numeric_and_product_inputs_fail_closed(policy: Stage2Policy) -> None:
    floated = _assess(policy, proposals=(proposal(entry_price=10.5),))
    assert has_reason(floated.budget_findings, Reason.FLOAT_REJECTED)
    assert floated.mathematical_budget_compliant is False
    negative = _assess(policy, proposals=(proposal(entry_price=Decimal("-1")),))
    assert has_reason(negative.budget_findings, Reason.NOT_POSITIVE) or has_reason(
        negative.budget_findings, Reason.NEGATIVE_REJECTED
    )
    nonfinite = _assess(policy, proposals=(proposal(fx_rate_to_gbp=Decimal("Infinity")),))
    assert has_reason(nonfinite.budget_findings, Reason.NONFINITE)
    missing_stop = _assess(policy, proposals=(proposal(stop_price=None, stop_protection=StopProtection.MISSING),))
    assert has_reason(missing_stop.budget_findings, Reason.STOP_MISSING_OR_UNKNOWN)
    unknown_stop = _assess(policy, proposals=(proposal(stop_protection=StopProtection.UNKNOWN),))
    assert has_reason(unknown_stop.budget_findings, Reason.STOP_MISSING_OR_UNKNOWN)
    partial_cover = _assess(policy, proposals=(proposal(covered_quantity=Decimal("0.5"), quantity_precision=1),))
    assert has_reason(partial_cover.budget_findings, Reason.STOP_COVERAGE_INCOMPLETE)
    short = _assess(policy, proposals=(proposal(side="SHORT"),))
    assert has_reason(short.budget_findings, Reason.SIDE_NOT_ALLOWED)
    cfd = _assess(policy, proposals=(proposal(product="CFD"),))
    assert has_reason(cfd.budget_findings, Reason.PRODUCT_NOT_ALLOWED)
    borrowed = _assess(policy, proposals=(proposal(borrowing=True),))
    assert has_reason(borrowed.budget_findings, Reason.BORROWING_NOT_ALLOWED)
    levered = _assess(policy, proposals=(proposal(leverage=Decimal("2")),))
    assert has_reason(levered.budget_findings, Reason.LEVERAGE_NOT_ALLOWED)
    for result in (floated, missing_stop, short, cfd):
        assert result.execution_permission_issued is False


def test_opening_baseline_failures_block_entries_and_not_protective_exits(policy: Stage2Policy) -> None:
    previous = _assess(
        policy,
        account=account(baseline_kind=BaselineKind.PREVIOUS_CLOSE, opening_freshness=Freshness.PREVIOUS_CLOSE),
    )
    assert has_reason(previous.budget_findings, Reason.OPENING_BASELINE_REJECTED)
    assert previous.mathematical_budget_compliant is False
    stale = _assess(policy, account=account(opening_freshness=Freshness.STALE))
    assert has_reason(stale.budget_findings, Reason.OPENING_BASELINE_REJECTED)
    absent = _assess(
        policy,
        account=account(
            baseline_kind=BaselineKind.ABSENT, opening_equity_gbp=None, opening_london_date=None, opening_freshness=Freshness.ABSENT
        ),
    )
    assert has_reason(absent.budget_findings, Reason.OPENING_BASELINE_REJECTED)
    unknown = _assess(policy, account=account(baseline_kind=BaselineKind.UNKNOWN, opening_freshness=Freshness.UNKNOWN))
    assert has_reason(unknown.budget_findings, Reason.OPENING_BASELINE_REJECTED)
    cost = _assess(policy, account=account(baseline_kind=BaselineKind.COST_BASIS, opening_freshness=Freshness.COST_BASIS))
    assert has_reason(cost.budget_findings, Reason.OPENING_BASELINE_REJECTED)
    stale_equity = _assess(policy, account=account(equity_freshness=Freshness.STALE))
    assert has_reason(stale_equity.budget_findings, Reason.EQUITY_NOT_FRESH)
    held = open_slice("E1")
    protective = assess_protective_exit(
        ProtectiveExitProposal(
            proposal_id="X1",
            episode_id="E1",
            instrument_id="SYNTH-EQ",
            quantity=Decimal("1"),
            quantity_precision=0,
            reduce_only=True,
            blind_liquidation=False,
        ),
        (held,),
        baseline_kind=BaselineKind.ABSENT,
    )
    assert protective.structure_ok is True
    assert protective.blocked_because_opening_baseline_missing is False
    assert protective.executed is False
    assert protective.entry_permission_issued is False
    assert protective.blind_liquidation_performed is False
    assert not has_reason(protective.findings, Reason.OPENING_BASELINE_REJECTED)
    liquidate = assess_protective_exit(
        ProtectiveExitProposal(
            proposal_id="X2",
            episode_id="E1",
            instrument_id="SYNTH-EQ",
            quantity=Decimal("1"),
            quantity_precision=0,
            reduce_only=False,
            blind_liquidation=True,
        ),
        (held,),
        baseline_kind=BaselineKind.FIRST_FRESH_OPENING,
    )
    assert liquidate.structure_ok is False
    assert has_reason(liquidate.findings, Reason.BLIND_LIQUIDATION_FORBIDDEN)
    assert has_reason(liquidate.findings, Reason.NOT_REDUCE_ONLY)
    assert liquidate.blind_liquidation_performed is False


def test_supplied_halt_flags_block_after_point_in_time_recovery(policy: Stage2Policy) -> None:
    recovered = _assess(
        policy,
        account=account(daily_halt_latched=True, loss_streak_halt_latched=True),
        closed_episodes=(
            ClosedEpisode("C1", "FILL-1", Decimal("-1.00"), False),
            ClosedEpisode("C2", "FILL-2", Decimal("-1.00"), False),
            ClosedEpisode("C3", "FILL-3", Decimal("-1.00"), False),
            ClosedEpisode("C4", "FILL-4", Decimal("0.20"), False),
        ),
        proposals=(proposal(episode_id="E9", stop_price=Decimal("9.90")),),
    )
    assert recovered.figures is not None
    assert recovered.figures.day_loss_gbp == Decimal("0.00")
    assert recovered.point_in_time_day_loss_blocks is False
    assert recovered.point_in_time_loss_streak == 0
    assert recovered.point_in_time_loss_streak_blocks is False
    assert recovered.mathematical_budget_compliant is True
    assert recovered.supplied_daily_halt_blocks is True
    assert recovered.supplied_loss_streak_halt_blocks is True
    assert recovered.execution_permission_issued is False
    live_streak = _assess(
        policy,
        closed_episodes=(
            ClosedEpisode("C1", "FILL-1", Decimal("-1.00"), False),
            ClosedEpisode("C2", "FILL-2", Decimal("-0.20"), False),
            ClosedEpisode("C3", "FILL-3", Decimal("-0.01"), False),
        ),
        proposals=(proposal(episode_id="E9", stop_price=Decimal("9.90")),),
    )
    assert live_streak.point_in_time_loss_streak == 3
    assert live_streak.point_in_time_loss_streak_blocks is True
    assert live_streak.mathematical_budget_compliant is True
    assert live_streak.supplied_loss_streak_halt_blocks is False
    partial = _assess(
        policy,
        closed_episodes=(ClosedEpisode("C1", "FILL-1", Decimal("-1.00"), True),),
    )
    assert has_reason(partial.budget_findings, Reason.PARTIAL_NOT_AN_EPISODE)


def test_unapproved_quotes_deny_and_fixtures_are_not_approval(policy: Stage2Policy) -> None:
    fresh = _assess(
        policy,
        quote=QuoteInput(Decimal("10"), Decimal("10.01"), AS_OF - timedelta(seconds=1), "DESK", "GOOD"),
        fx_quote=FxQuoteInput(Decimal("1"), AS_OF - timedelta(seconds=1), "DESK-FX"),
        session=SessionInput("LSE", "REGULAR", True),
    )
    assert fresh.mathematical_budget_compliant is True
    assert fresh.operational_structure_ok is False
    assert has_reason(fresh.operational_findings, Reason.SETTINGS_UNAPPROVED_UNCONFIGURED)
    assert has_reason(fresh.operational_findings, Reason.SESSION_UNAPPROVED_UNCONFIGURED)
    assert not has_reason(fresh.operational_findings, Reason.QUOTE_STALE)
    assert fresh.execution_permission_issued is False

    limits = fixture_limits(15)
    stale = _assess(
        policy,
        quote=fresh_quote(16),
        fx_quote=fresh_fx(0),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(stale.operational_findings, Reason.QUOTE_STALE)
    assert has_reason(stale.operational_findings, Reason.FIXTURE_NOT_OWNER_APPROVED)
    assert stale.operational_settings_approval is SettingsApproval.FIXTURE_NOT_OWNER_APPROVED
    assert stale.operationally_eligible_for_execution is False

    future = _assess(
        policy,
        quote=QuoteInput(Decimal("10"), Decimal("10.01"), AS_OF + timedelta(seconds=1), "FIXTURE-SOURCE", "FIXTURE-QUALITY"),
        fx_quote=fresh_fx(0),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(future.operational_findings, Reason.QUOTE_TIMESTAMP_FUTURE)
    naive = _assess(
        policy,
        quote=QuoteInput(Decimal("10"), Decimal("10.01"), datetime(2026, 6, 1, 10, 0), "FIXTURE-SOURCE", "FIXTURE-QUALITY"),
        fx_quote=fresh_fx(0),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(naive.operational_findings, Reason.QUOTE_TIMESTAMP_NAIVE)
    negative = _assess(
        policy,
        quote=QuoteInput(Decimal("-1"), Decimal("10"), AS_OF, "FIXTURE-SOURCE", "FIXTURE-QUALITY"),
        fx_quote=fresh_fx(0),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(negative.operational_findings, Reason.QUOTE_NOT_POSITIVE) or has_reason(
        negative.operational_findings, Reason.NEGATIVE_REJECTED
    )
    nonfinite = _assess(
        policy,
        quote=QuoteInput(Decimal("NaN"), Decimal("10"), AS_OF, "FIXTURE-SOURCE", "FIXTURE-QUALITY"),
        fx_quote=fresh_fx(0),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(nonfinite.operational_findings, Reason.NONFINITE)
    floated = _assess(
        policy,
        quote=QuoteInput(10.0, Decimal("10"), AS_OF, "FIXTURE-SOURCE", "FIXTURE-QUALITY"),  # type: ignore[arg-type]
        fx_quote=fresh_fx(0),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(floated.operational_findings, Reason.FLOAT_REJECTED)
    bad_source = _assess(
        policy,
        quote=QuoteInput(Decimal("10"), Decimal("10.01"), AS_OF, "OTHER", "FIXTURE-QUALITY"),
        fx_quote=fresh_fx(0),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(bad_source.operational_findings, Reason.QUOTE_SOURCE_REJECTED)
    missing_session = _assess(
        policy,
        quote=fresh_quote(0),
        fx_quote=fresh_fx(0),
        session=SessionInput(None, None, None),
        fixture_limits=limits,
    )
    assert has_reason(missing_session.operational_findings, Reason.SESSION_OR_CALENDAR_MISSING)
    rollback = _assess(
        policy,
        clock=ClockInput(as_of=AS_OF, previous_as_of=AS_OF + timedelta(seconds=5)),
        quote=fresh_quote(0),
        fx_quote=fresh_fx(0),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(rollback.operational_findings, Reason.CLOCK_ROLLBACK)
    naive_clock = _assess(policy, clock=ClockInput(as_of=datetime(2026, 6, 1, 10, 0)))
    assert has_reason(naive_clock.operational_findings, Reason.CLOCK_NAIVE)
    structured = _assess(
        policy,
        quote=fresh_quote(0),
        fx_quote=fresh_fx(0),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert structured.operational_structure_ok is True
    assert structured.mathematical_budget_compliant is True
    assert structured.operationally_eligible_for_execution is False
    assert structured.execution_permission_issued is False
    assert has_reason(structured.operational_findings, Reason.FIXTURE_NOT_OWNER_APPROVED)
    assert has_reason(structured.operational_findings, Reason.ALLOWLIST_EMPTY)


def test_permission_bits_cannot_be_forced_true() -> None:
    with pytest.raises(RiskInputError):
        EntryAssessment(
            mathematical_budget_compliant=True,
            budget_findings=(),
            operational_structure_ok=True,
            operational_findings=(),
            operational_settings_approval=SettingsApproval.UNAPPROVED_UNCONFIGURED,
            operationally_eligible_for_execution=True,  # type: ignore[arg-type]
            execution_permission_issued=False,
            ticket_issued=False,
            allowlist_permitted=False,
            runtime_enforced=False,
            supplied_daily_halt_blocks=False,
            supplied_loss_streak_halt_blocks=False,
            day_loss_known=True,
            point_in_time_day_loss_blocks=False,
            point_in_time_loss_streak=0,
            point_in_time_loss_streak_blocks=False,
            planned_cap_guarantees_realised_loss=False,
            stop_contract_proves_protection=False,
            figures=None,
            boundary=(),
        )


def test_conservative_price_cannot_understate_notional(policy: Stage2Policy) -> None:
    result = _assess(policy, proposals=(proposal(conservative_price=Decimal("9.00")),))
    assert has_reason(result.budget_findings, Reason.CONSERVATIVE_BELOW_REFERENCE)
    assert result.mathematical_budget_compliant is False


def test_more_fail_closed_inputs(policy: Stage2Policy) -> None:
    live = _assess(policy, account=account(live=True))
    assert has_reason(live.budget_findings, Reason.LIVE_INPUT_REJECTED)
    foreign = _assess(policy, account=account(currency="USD"))
    assert has_reason(foreign.budget_findings, Reason.CURRENCY_REJECTED)
    wrong_day = _assess(
        policy,
        account=account(as_of_london_date=date(2026, 6, 2), opening_london_date=date(2026, 6, 2)),
    )
    assert has_reason(wrong_day.budget_findings, Reason.CLOCK_DATE_MISMATCH)
    negative_cash = _assess(policy, account=account(available_cash_gbp=Decimal("-0.01")))
    assert has_reason(negative_cash.budget_findings, Reason.NEGATIVE_REJECTED)
    empty = assess_long_entries(policy=policy, account=account(), clock=policy_clock(), proposals=())
    assert has_reason(empty.budget_findings, Reason.MISSING)
    unmarked = _assess(policy, positions=(replace(open_slice("E1"), mark_price=None),))
    assert has_reason(unmarked.budget_findings, Reason.MARK_UNKNOWN)
    fx = _assess(
        policy,
        proposals=(proposal(fx_rate_to_gbp=Decimal("1.10"), conservative_fx_rate_to_gbp=Decimal("1.10")),),
    )
    assert has_reason(fx.budget_findings, Reason.FX_NOT_ONE_FOR_GBP)
    missing_episode = _assess(policy, proposals=(proposal(episode_id="E-missing", opens_new_episode=False),))
    assert has_reason(missing_episode.budget_findings, Reason.EPISODE_NOT_OPEN)
    duplicate = _assess(
        policy,
        proposals=(proposal(proposal_id="P1", episode_id="E1"), proposal(proposal_id="P1", episode_id="E2")),
    )
    assert has_reason(duplicate.budget_findings, Reason.DUPLICATE_PROPOSAL)
    usd = _assess(
        policy,
        proposals=(
            proposal(
                instrument_currency="USD",
                fx_rate_to_gbp=Decimal("0.80"),
                conservative_fx_rate_to_gbp=Decimal("0.80"),
                entry_price=Decimal("10"),
                stop_price=Decimal("9.90"),
                conservative_price=Decimal("10"),
            ),
        ),
    )
    assert usd.mathematical_budget_compliant is True
    assert usd.figures is not None
    assert usd.figures.proposal_planned_risk_gbp == (Decimal("0.08"),)
    too_big = assess_protective_exit(
        ProtectiveExitProposal("X", "E1", "SYNTH-EQ", Decimal("2"), 0, True, False),
        (open_slice("E1"),),
        baseline_kind=BaselineKind.ABSENT,
    )
    assert has_reason(too_big.findings, Reason.PROTECTIVE_QUANTITY_EXCEEDS_POSITION)
    assert too_big.blocked_because_opening_baseline_missing is False
    limits = fixture_limits(30)
    rejected_session = _assess(
        policy,
        quote=fresh_quote(0),
        fx_quote=fresh_fx(0),
        session=SessionInput("OTHER-CAL", "OTHER-SESSION", False),
        fixture_limits=limits,
    )
    assert has_reason(rejected_session.operational_findings, Reason.CALENDAR_REJECTED)
    assert has_reason(rejected_session.operational_findings, Reason.SESSION_REJECTED)
    assert has_reason(rejected_session.operational_findings, Reason.SESSION_NOT_CONFIRMED)
    bad_fx_source = _assess(
        policy,
        quote=fresh_quote(0),
        fx_quote=FxQuoteInput(Decimal("1"), AS_OF, "OTHER-FX"),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(bad_fx_source.operational_findings, Reason.FX_SOURCE_REJECTED)
    missing_quality = _assess(
        policy,
        quote=QuoteInput(Decimal("10"), Decimal("10"), AS_OF, "FIXTURE-SOURCE", None),
        fx_quote=fresh_fx(0),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(missing_quality.operational_findings, Reason.QUOTE_QUALITY_MISSING)
    assert has_reason(_assess(policy, clock=ClockInput(as_of=None)).operational_findings, Reason.CLOCK_MISSING)


def test_fx_quote_staleness_uses_only_the_fixture_age(policy: Stage2Policy) -> None:
    limits = fixture_limits(10)
    result = _assess(
        policy,
        quote=fresh_quote(0),
        fx_quote=FxQuoteInput(Decimal("1"), AS_OF - timedelta(seconds=11), "FIXTURE-FX"),
        session=fresh_session(),
        fixture_limits=limits,
    )
    assert has_reason(result.operational_findings, Reason.FX_STALE)
    assert result.operationally_eligible_for_execution is False

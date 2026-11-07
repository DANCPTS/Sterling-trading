"""Fail-closed checks for Stage 2 Gate 1 inputs.

Mathematical budget compliance is computed only from validated decimals.
Operational quote, FX, session, and source checks are reported separately.
This module does not issue an execution permission, a ticket, or a halt latch,
and it does not treat an accounting valuation label as an execution quote.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sterling.errors import MoneyError
from sterling.ledger.calendar import london_date
from sterling.ledger.money import require_money, require_scale, signed_money
from sterling.risk.budget import (
    aggregate_risk_cap,
    cash_required_gbp,
    concentration_cap,
    consecutive_net_loss_episodes,
    conservative_notional_gbp,
    daily_loss_threshold_cap,
    day_loss_gbp,
    incremental_stop_risk_gbp,
    per_trade_risk_cap,
    planned_entry_risk_gbp,
    remaining_day_risk_gbp,
)
from sterling.risk.contracts import (
    BOUNDARY_STATEMENTS,
    AccountInput,
    BaselineKind,
    BookPosition,
    BudgetFigures,
    ClockInput,
    ClosedEpisode,
    EntryAssessment,
    EntryProposal,
    Finding,
    FixtureOperationalLimits,
    Freshness,
    FxQuoteInput,
    KnownStatus,
    PositionStatus,
    ProtectiveExitAssessment,
    ProtectiveExitProposal,
    QuoteInput,
    SessionInput,
    SettingsApproval,
    StopProtection,
)
from sterling.risk.policy import Stage2Policy
from sterling.risk.reasons import Reason

ZERO = Decimal("0.00")
_ONE = Decimal("1")
_APPROVAL_ONLY = frozenset({Reason.FIXTURE_NOT_OWNER_APPROVED, Reason.ALLOWLIST_EMPTY})


@dataclass(frozen=True, slots=True)
class _Slice:
    episode_id: str
    instrument_id: str
    status: PositionStatus
    risk_gbp: Decimal
    notional_gbp: Decimal


def assess_long_entries(
    *,
    policy: Stage2Policy,
    account: AccountInput,
    clock: ClockInput,
    proposals: tuple[EntryProposal, ...],
    positions: tuple[BookPosition, ...] = (),
    closed_episodes: tuple[ClosedEpisode, ...] = (),
    quote: QuoteInput | None = None,
    fx_quote: FxQuoteInput | None = None,
    session: SessionInput | None = None,
    fixture_limits: FixtureOperationalLimits | None = None,
) -> EntryAssessment:
    """Assess aggregate long entries. Pending openings in ``positions`` count.

    The caller must pass every concurrent proposal. Gate 1 does not reserve
    an episode or a cash amount, so a proposal left out of this aggregate is
    invisible to the count.
    """
    budget: list[Finding] = []
    operational = _operational_findings(
        clock=clock,
        quote=quote,
        fx_quote=fx_quote,
        session=session,
        fixture_limits=fixture_limits,
    )
    can_price = True

    if account.live is not False:
        budget.append(Finding(Reason.LIVE_INPUT_REJECTED, "account.live"))
        can_price = False
    if account.simulated is not True:
        budget.append(Finding(Reason.SIMULATED_REQUIRED, "account.simulated"))
        can_price = False
    if account.currency != "GBP":
        budget.append(Finding(Reason.CURRENCY_REJECTED, "account.currency"))
        can_price = False
    if not isinstance(account.daily_halt_latched, bool) or not isinstance(account.loss_streak_halt_latched, bool):
        budget.append(Finding(Reason.INVALID_DECIMAL, "halt_flags"))
        can_price = False

    london = _london_day(clock, account, budget)
    if london is False:
        can_price = False

    equity = _parse(account.marked_equity_gbp, detail="marked_equity_gbp", mode="penny_non_negative", findings=budget)
    opening = _parse(account.opening_equity_gbp, detail="opening_equity_gbp", mode="penny_non_negative", findings=budget)
    net = _parse(
        account.net_external_deposits_gbp,
        detail="net_external_deposits_gbp",
        mode="penny_signed",
        findings=budget,
    )
    if equity is None or opening is None or net is None:
        can_price = False

    if account.equity_freshness != Freshness.FRESH_VALIDATED:
        budget.append(Finding(Reason.EQUITY_NOT_FRESH, _enum_detail(account.equity_freshness)))
        can_price = False
    if not _baseline_ok(account, clock, budget):
        can_price = False

    cash_known, cash_amount = _cash(account, budget)
    if not cash_known:
        can_price = False
    if account.gross_exposure_status != KnownStatus.KNOWN:
        budget.append(Finding(Reason.EXPOSURE_UNKNOWN, "gross_exposure"))

    slices, episode_instruments, episode_states, priced = _positions(positions, budget)
    if not priced:
        can_price = False
    streak = _closed_episodes(closed_episodes, episode_instruments, budget)
    if streak is None:
        can_price = False

    if not isinstance(proposals, tuple):
        budget.append(Finding(Reason.INVALID_DECIMAL, "proposals"))
        can_price = False
        proposals = ()
    if len(proposals) == 0:
        budget.append(Finding(Reason.MISSING, "proposals"))
        can_price = False

    proposal_rows, proposal_ok = _proposals(
        proposals,
        episode_instruments=episode_instruments,
        episode_states=episode_states,
        findings=budget,
    )
    if not proposal_ok:
        can_price = False

    figures: BudgetFigures | None = None
    day_blocks = False
    if can_price and equity is not None and opening is not None and net is not None and streak is not None and cash_known:
        figures, day_blocks = _apply_caps(
            policy=policy,
            equity=equity,
            opening=opening,
            net=net,
            cash_amount=cash_amount,
            slices=slices,
            proposal_rows=proposal_rows,
            episode_states=episode_states,
            streak=streak,
            findings=budget,
        )

    structure_ok = not any(item.reason not in _APPROVAL_ONLY for item in operational)
    approval = (
        SettingsApproval.FIXTURE_NOT_OWNER_APPROVED if fixture_limits is not None else SettingsApproval.UNAPPROVED_UNCONFIGURED
    )
    return EntryAssessment(
        mathematical_budget_compliant=can_price and not budget,
        budget_findings=tuple(budget),
        operational_structure_ok=structure_ok,
        operational_findings=tuple(operational),
        operational_settings_approval=approval,
        operationally_eligible_for_execution=False,
        execution_permission_issued=False,
        ticket_issued=False,
        allowlist_permitted=False,
        runtime_enforced=False,
        supplied_daily_halt_blocks=account.daily_halt_latched is True,
        supplied_loss_streak_halt_blocks=account.loss_streak_halt_latched is True,
        day_loss_known=figures is not None,
        point_in_time_day_loss_blocks=day_blocks,
        point_in_time_loss_streak=streak,
        point_in_time_loss_streak_blocks=streak is not None and streak >= policy.consecutive_loss_episode_threshold,
        planned_cap_guarantees_realised_loss=False,
        stop_contract_proves_protection=False,
        figures=figures,
        boundary=BOUNDARY_STATEMENTS,
    )


def assess_protective_exit(
    proposal: ProtectiveExitProposal,
    positions: tuple[BookPosition, ...],
    *,
    baseline_kind: BaselineKind,
) -> ProtectiveExitAssessment:
    """Check a reduce-only exit intent.

    An absent, stale, or previous-close opening baseline does not block this
    check and does not create a liquidation. No order is submitted.
    """
    findings: list[Finding] = []
    if not isinstance(baseline_kind, BaselineKind):
        findings.append(Finding(Reason.INVALID_DECIMAL, "baseline_kind"))
    if proposal.blind_liquidation is not False:
        findings.append(Finding(Reason.BLIND_LIQUIDATION_FORBIDDEN, "blind_liquidation"))
    if proposal.reduce_only is not True:
        findings.append(Finding(Reason.NOT_REDUCE_ONLY, "reduce_only"))
    if _blank(proposal.proposal_id) or _blank(proposal.episode_id) or _blank(proposal.instrument_id):
        findings.append(Finding(Reason.MISSING, "protective_identity"))
    quantity = _parse(
        proposal.quantity,
        detail="protective.quantity",
        mode="positive",
        places=proposal.quantity_precision if _precision_ok(proposal.quantity_precision) else 8,
        findings=findings,
    )
    if not _precision_ok(proposal.quantity_precision):
        findings.append(Finding(Reason.PRECISION_INVALID, "protective.quantity_precision"))
    open_quantity = ZERO
    found = False
    for position in positions:
        if (
            position.episode_id == proposal.episode_id
            and position.instrument_id == proposal.instrument_id
            and position.status == PositionStatus.OPEN
            and isinstance(position.quantity, Decimal)
            and position.quantity.is_finite()
            and position.quantity > 0
        ):
            found = True
            open_quantity += position.quantity
    if not found:
        findings.append(Finding(Reason.PROTECTIVE_POSITION_NOT_OPEN, proposal.episode_id))
    elif quantity is not None and quantity > open_quantity:
        findings.append(Finding(Reason.PROTECTIVE_QUANTITY_EXCEEDS_POSITION, proposal.episode_id))
    return ProtectiveExitAssessment(
        structure_ok=not findings,
        findings=tuple(findings),
        entry_permission_issued=False,
        executed=False,
        blind_liquidation_performed=False,
        blocked_because_opening_baseline_missing=False,
        stop_contract_proves_protection=False,
        boundary=BOUNDARY_STATEMENTS,
    )


def _apply_caps(
    *,
    policy: Stage2Policy,
    equity: Decimal,
    opening: Decimal,
    net: Decimal,
    cash_amount: Decimal | None,
    slices: tuple[_Slice, ...],
    proposal_rows: tuple[tuple[EntryProposal, Decimal, Decimal, Decimal], ...],
    episode_states: dict[str, set[PositionStatus]],
    streak: int,
    findings: list[Finding],
) -> tuple[BudgetFigures, bool]:
    per_trade = per_trade_risk_cap(equity, policy)
    threshold = daily_loss_threshold_cap(opening, policy)
    loss = day_loss_gbp(opening, equity, net)
    existing = ZERO
    notionals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for row in slices:
        if row.status == PositionStatus.FLAT:
            continue
        existing += row.risk_gbp
        notionals[row.instrument_id] += row.notional_gbp
    remaining = remaining_day_risk_gbp(threshold, loss, existing)
    proposal_risk = tuple(row[1] for row in proposal_rows)
    proposal_notional = tuple(row[2] for row in proposal_rows)
    proposal_cash = tuple(row[3] for row in proposal_rows)
    added_risk = sum(proposal_risk, start=ZERO)
    cash_required = sum(proposal_cash, start=ZERO)
    for proposal, _risk, notional, _cash in proposal_rows:
        notionals[proposal.instrument_id] += notional
    active = [episode_id for episode_id, states in episode_states.items() if _active(states)]
    for proposal, _risk, _notional, _cash in proposal_rows:
        if proposal.opens_new_episode and proposal.episode_id not in active:
            active.append(proposal.episode_id)
    episode_count = len(active)
    day_blocks = loss >= threshold
    if day_blocks:
        findings.append(Finding(Reason.DAY_LOSS_AT_OR_ABOVE_THRESHOLD, "day_loss"))
    for proposal, risk, _notional, _cash in proposal_rows:
        if risk > per_trade:
            findings.append(Finding(Reason.PER_TRADE_CAP_EXCEEDED, proposal.proposal_id))
    if added_risk > remaining:
        findings.append(Finding(Reason.DAILY_REMAINING_EXCEEDED, "remaining_day_risk"))
    if existing + added_risk > aggregate_risk_cap(equity, policy):
        findings.append(Finding(Reason.AGGREGATE_RISK_EXCEEDED, "aggregate"))
    cap = concentration_cap(equity, policy)
    for instrument_id, notional in sorted(notionals.items()):
        if notional > cap:
            findings.append(Finding(Reason.CONCENTRATION_EXCEEDED, instrument_id))
    if episode_count > policy.max_simultaneous_position_episodes:
        findings.append(Finding(Reason.EPISODE_CAP_EXCEEDED, str(episode_count)))
    if cash_amount is not None and cash_required > cash_amount:
        findings.append(Finding(Reason.CASH_EXCEEDED, "available_cash"))
    figures = BudgetFigures(
        per_trade_cap_gbp=per_trade,
        daily_threshold_gbp=threshold,
        day_loss_gbp=loss,
        existing_incremental_risk_gbp=existing,
        remaining_day_risk_gbp=remaining,
        aggregate_cap_gbp=aggregate_risk_cap(equity, policy),
        aggregate_used_gbp=existing + added_risk,
        concentration_cap_gbp=cap,
        proposal_planned_risk_gbp=proposal_risk,
        proposal_notional_gbp=proposal_notional,
        proposal_cash_required_gbp=proposal_cash,
        instrument_notional_gbp=tuple(sorted(notionals.items())),
        episode_count=episode_count,
        episode_cap=policy.max_simultaneous_position_episodes,
        available_cash_gbp=cash_amount,
        cash_required_gbp=cash_required,
        consecutive_loss_episodes=streak,
    )
    return figures, day_blocks


def _positions(
    positions: tuple[BookPosition, ...],
    findings: list[Finding],
) -> tuple[tuple[_Slice, ...], dict[str, str], dict[str, set[PositionStatus]], bool]:
    slices: list[_Slice] = []
    instruments: dict[str, str] = {}
    states: dict[str, set[PositionStatus]] = defaultdict(set)
    ok = True
    for index, position in enumerate(positions):
        detail = f"positions[{index}]"
        if _blank(position.episode_id) or _blank(position.instrument_id):
            findings.append(Finding(Reason.MISSING, detail))
            ok = False
            continue
        if position.episode_id in instruments and instruments[position.episode_id] != position.instrument_id:
            findings.append(Finding(Reason.EPISODE_INCONSISTENT, position.episode_id))
            ok = False
        instruments[position.episode_id] = position.instrument_id
        states[position.episode_id].add(position.status)
        if position.status == PositionStatus.FLAT:
            quantity = _parse(position.quantity, detail=f"{detail}.quantity", mode="non_negative", places=8, findings=findings)
            if quantity is None or quantity != 0:
                findings.append(Finding(Reason.EPISODE_INCONSISTENT, position.episode_id))
                ok = False
            continue
        row = _live_slice(position, detail, findings)
        if row is None:
            ok = False
            continue
        slices.append(row)
    for episode_id, episode_states in states.items():
        if PositionStatus.FLAT in episode_states and _active(episode_states):
            findings.append(Finding(Reason.EPISODE_INCONSISTENT, episode_id))
            ok = False
    return tuple(slices), instruments, states, ok


def _live_slice(position: BookPosition, detail: str, findings: list[Finding]) -> _Slice | None:
    local: list[Finding] = []
    quantity = _quantity(position.quantity, position.quantity_precision, local, f"{detail}.quantity")
    entry = _parse(position.entry_price, detail=f"{detail}.entry_price", mode="positive", findings=local)
    fx = _parse(position.fx_rate_to_gbp, detail=f"{detail}.fx_rate_to_gbp", mode="positive", findings=local)
    _gbp_fx(position.instrument_currency, fx, None, local, detail)
    stop = _stop(
        protection=position.stop_protection,
        stop_price=position.stop_price,
        covered=position.covered_quantity,
        quantity=quantity,
        entry=entry,
        findings=local,
        detail=detail,
    )
    unbooked = _parse(
        position.unbooked_entry_cost_gbp,
        detail=f"{detail}.unbooked_entry_cost_gbp",
        mode="non_negative",
        findings=local,
    )
    exit_cost = _parse(
        position.expected_exit_cost_gbp,
        detail=f"{detail}.expected_exit_cost_gbp",
        mode="non_negative",
        findings=local,
    )
    adverse = _parse(
        position.adverse_allowance_gbp,
        detail=f"{detail}.adverse_allowance_gbp",
        mode="non_negative",
        findings=local,
    )
    booked = _parse(
        position.booked_entry_fees_gbp,
        detail=f"{detail}.booked_entry_fees_gbp",
        mode="non_negative",
        findings=local,
    )
    reference: Decimal | None
    if position.status == PositionStatus.PENDING_OPEN:
        reference = entry
    else:
        reference = _parse(position.mark_price, detail=f"{detail}.mark_price", mode="positive", findings=local)
        if position.mark_price is None:
            local.append(Finding(Reason.MARK_UNKNOWN, detail))
    conservative = _parse(
        position.conservative_price,
        detail=f"{detail}.conservative_price",
        mode="positive",
        findings=local,
    )
    conservative_fx = _parse(
        position.conservative_fx_rate_to_gbp,
        detail=f"{detail}.conservative_fx",
        mode="positive",
        findings=local,
    )
    _gbp_fx(position.instrument_currency, None, conservative_fx, local, detail)
    if reference is not None and conservative is not None and conservative < reference:
        local.append(Finding(Reason.CONSERVATIVE_BELOW_REFERENCE, f"{detail}.conservative_price"))
    if fx is not None and conservative_fx is not None and conservative_fx < fx:
        local.append(Finding(Reason.CONSERVATIVE_BELOW_REFERENCE, f"{detail}.conservative_fx"))
    if local or None in (quantity, entry, fx, stop, unbooked, exit_cost, adverse, booked, reference, conservative, conservative_fx):
        findings.extend(local)
        return None
    assert quantity is not None and stop is not None and fx is not None
    assert unbooked is not None and exit_cost is not None and adverse is not None
    assert reference is not None and conservative is not None and conservative_fx is not None
    assert booked is not None
    del booked  # Recorded only so a booked fee cannot be mistaken for incremental risk.
    risk = incremental_stop_risk_gbp(
        reference_price=reference,
        stop_price=stop,
        quantity=quantity,
        fx_rate_to_gbp=fx,
        unbooked_entry_cost_gbp=unbooked,
        expected_exit_cost_gbp=exit_cost,
        adverse_allowance_gbp=adverse,
    )
    notional = conservative_notional_gbp(quantity=quantity, price=conservative, fx_rate_to_gbp=conservative_fx)
    return _Slice(
        episode_id=position.episode_id,
        instrument_id=position.instrument_id,
        status=position.status,
        risk_gbp=risk,
        notional_gbp=notional,
    )


def _proposals(
    proposals: tuple[EntryProposal, ...],
    *,
    episode_instruments: dict[str, str],
    episode_states: dict[str, set[PositionStatus]],
    findings: list[Finding],
) -> tuple[tuple[tuple[EntryProposal, Decimal, Decimal, Decimal], ...], bool]:
    rows: list[tuple[EntryProposal, Decimal, Decimal, Decimal]] = []
    seen_ids: set[str] = set()
    claimed: set[str] = set()
    ok = True
    for index, proposal in enumerate(proposals):
        detail = proposal.proposal_id or f"proposals[{index}]"
        local: list[Finding] = []
        if _blank(proposal.proposal_id):
            local.append(Finding(Reason.MISSING, "proposal_id"))
        elif proposal.proposal_id in seen_ids:
            local.append(Finding(Reason.DUPLICATE_PROPOSAL, proposal.proposal_id))
        else:
            seen_ids.add(proposal.proposal_id)
        if _blank(proposal.episode_id) or _blank(proposal.instrument_id):
            local.append(Finding(Reason.MISSING, f"{detail}.identity"))
        if proposal.product != "LONG_EQUITY":
            local.append(Finding(Reason.PRODUCT_NOT_ALLOWED, proposal.product))
        if proposal.side != "BUY":
            local.append(Finding(Reason.SIDE_NOT_ALLOWED, proposal.side))
        if proposal.borrowing is not False:
            local.append(Finding(Reason.BORROWING_NOT_ALLOWED, detail))
        leverage = _parse(proposal.leverage, detail=f"{detail}.leverage", mode="positive", findings=local)
        if leverage is not None and leverage != _ONE:
            local.append(Finding(Reason.LEVERAGE_NOT_ALLOWED, detail))
        quantity = _quantity(proposal.quantity, proposal.quantity_precision, local, f"{detail}.quantity")
        entry = _parse(proposal.entry_price, detail=f"{detail}.entry_price", mode="positive", findings=local)
        fx = _parse(proposal.fx_rate_to_gbp, detail=f"{detail}.fx_rate_to_gbp", mode="positive", findings=local)
        _gbp_fx(proposal.instrument_currency, fx, None, local, detail)
        stop = _stop(
            protection=proposal.stop_protection,
            stop_price=proposal.stop_price,
            covered=proposal.covered_quantity,
            quantity=quantity,
            entry=entry,
            findings=local,
            detail=detail,
        )
        entry_cost = _parse(
            proposal.expected_entry_cost_gbp,
            detail=f"{detail}.expected_entry_cost_gbp",
            mode="non_negative",
            findings=local,
        )
        exit_cost = _parse(
            proposal.expected_exit_cost_gbp,
            detail=f"{detail}.expected_exit_cost_gbp",
            mode="non_negative",
            findings=local,
        )
        adverse = _parse(
            proposal.adverse_allowance_gbp,
            detail=f"{detail}.adverse_allowance_gbp",
            mode="non_negative",
            findings=local,
        )
        conservative = _parse(
            proposal.conservative_price,
            detail=f"{detail}.conservative_price",
            mode="positive",
            findings=local,
        )
        conservative_fx = _parse(
            proposal.conservative_fx_rate_to_gbp,
            detail=f"{detail}.conservative_fx",
            mode="positive",
            findings=local,
        )
        _gbp_fx(proposal.instrument_currency, None, conservative_fx, local, detail)
        if entry is not None and conservative is not None and conservative < entry:
            local.append(Finding(Reason.CONSERVATIVE_BELOW_REFERENCE, f"{detail}.conservative_price"))
        if fx is not None and conservative_fx is not None and conservative_fx < fx:
            local.append(Finding(Reason.CONSERVATIVE_BELOW_REFERENCE, f"{detail}.conservative_fx"))
        _proposal_episode(proposal, episode_instruments, episode_states, claimed, local)
        if local or None in (quantity, entry, fx, stop, entry_cost, exit_cost, adverse, conservative, conservative_fx):
            findings.extend(local)
            ok = False
            continue
        assert quantity is not None and entry is not None and fx is not None and stop is not None
        assert entry_cost is not None and exit_cost is not None and adverse is not None
        assert conservative is not None and conservative_fx is not None
        risk = planned_entry_risk_gbp(
            entry_price=entry,
            stop_price=stop,
            quantity=quantity,
            fx_rate_to_gbp=fx,
            expected_entry_cost_gbp=entry_cost,
            expected_exit_cost_gbp=exit_cost,
            adverse_allowance_gbp=adverse,
        )
        notional = conservative_notional_gbp(quantity=quantity, price=conservative, fx_rate_to_gbp=conservative_fx)
        cash = cash_required_gbp(
            quantity=quantity,
            entry_price=entry,
            fx_rate_to_gbp=fx,
            expected_entry_cost_gbp=entry_cost,
        )
        rows.append((proposal, risk, notional, cash))
    return tuple(rows), ok


def _proposal_episode(
    proposal: EntryProposal,
    instruments: dict[str, str],
    states: dict[str, set[PositionStatus]],
    claimed: set[str],
    findings: list[Finding],
) -> None:
    if _blank(proposal.episode_id):
        return
    known = proposal.episode_id in instruments
    active = known and _active(states.get(proposal.episode_id, set()))
    if proposal.opens_new_episode:
        if known or proposal.episode_id in claimed:
            findings.append(Finding(Reason.EPISODE_ID_REUSED, proposal.episode_id))
            return
        claimed.add(proposal.episode_id)
        return
    if not active:
        findings.append(Finding(Reason.EPISODE_NOT_OPEN, proposal.episode_id))
        return
    if instruments.get(proposal.episode_id) != proposal.instrument_id:
        findings.append(Finding(Reason.EPISODE_INCONSISTENT, proposal.episode_id))


def _closed_episodes(
    episodes: tuple[ClosedEpisode, ...],
    instruments: dict[str, str],
    findings: list[Finding],
) -> int | None:
    nets: list[Decimal] = []
    ok = True
    seen: set[str] = set()
    for index, episode in enumerate(episodes):
        detail = f"closed_episodes[{index}]"
        if episode.partial_exit is not False:
            findings.append(Finding(Reason.PARTIAL_NOT_AN_EPISODE, episode.episode_id or detail))
            ok = False
            continue
        if _blank(episode.episode_id) or _blank(episode.origin_fill_id):
            findings.append(Finding(Reason.MISSING, detail))
            ok = False
            continue
        if episode.episode_id in seen or episode.episode_id in instruments:
            findings.append(Finding(Reason.EPISODE_ID_REUSED, episode.episode_id))
            ok = False
        seen.add(episode.episode_id)
        net = _parse(episode.net_gbp, detail=f"{detail}.net_gbp", mode="penny_signed", findings=findings)
        if net is None:
            ok = False
            continue
        nets.append(net)
    if not ok:
        return None
    return consecutive_net_loss_episodes(nets)


def _cash(account: AccountInput, findings: list[Finding]) -> tuple[bool, Decimal | None]:
    """Return whether cash can be priced, and the known amount.

    ``(True, None)`` means the status is UNKNOWN, which still denies the
    budget but does not hide the other arithmetic. ``(False, None)`` means
    the supplied number itself is invalid.
    """
    if account.available_cash_status == KnownStatus.UNKNOWN:
        findings.append(Finding(Reason.CASH_UNKNOWN, "available_cash"))
        return True, None
    if account.available_cash_status != KnownStatus.KNOWN or account.available_cash_gbp is None:
        findings.append(Finding(Reason.CASH_UNKNOWN, "available_cash"))
        return True, None
    amount = _parse(account.available_cash_gbp, detail="available_cash_gbp", mode="penny_signed", findings=findings)
    if amount is None:
        return False, None
    if amount < 0:
        findings.append(Finding(Reason.NEGATIVE_REJECTED, "available_cash"))
        return False, None
    return True, amount


def _baseline_ok(account: AccountInput, clock: ClockInput, findings: list[Finding]) -> bool:
    if account.baseline_kind != BaselineKind.FIRST_FRESH_OPENING:
        findings.append(Finding(Reason.OPENING_BASELINE_REJECTED, _enum_detail(account.baseline_kind)))
        return False
    if account.opening_freshness != Freshness.FRESH_VALIDATED:
        findings.append(Finding(Reason.OPENING_BASELINE_REJECTED, _enum_detail(account.opening_freshness)))
        return False
    if account.opening_equity_gbp is None or account.opening_london_date is None:
        findings.append(Finding(Reason.OPENING_BASELINE_REJECTED, "ABSENT"))
        return False
    if account.opening_london_date != account.as_of_london_date:
        findings.append(Finding(Reason.OPENING_NOT_THIS_LONDON_DAY, "opening_london_date"))
        return False
    if isinstance(clock.as_of, datetime) and _aware(clock.as_of):
        if account.opening_london_date != london_date(clock.as_of):
            findings.append(Finding(Reason.OPENING_NOT_THIS_LONDON_DAY, "opening_london_date"))
            return False
    return True


def _london_day(clock: ClockInput, account: AccountInput, findings: list[Finding]) -> bool:
    if not isinstance(clock.as_of, datetime) or not _aware(clock.as_of):
        return True
    if account.as_of_london_date != london_date(clock.as_of):
        findings.append(Finding(Reason.CLOCK_DATE_MISMATCH, "as_of_london_date"))
        return False
    return True


def _stop(
    *,
    protection: StopProtection,
    stop_price: Decimal | None,
    covered: Decimal | None,
    quantity: Decimal | None,
    entry: Decimal | None,
    findings: list[Finding],
    detail: str,
) -> Decimal | None:
    if protection in {StopProtection.MISSING, StopProtection.UNKNOWN} or stop_price is None:
        findings.append(Finding(Reason.STOP_MISSING_OR_UNKNOWN, detail))
        return None
    if protection != StopProtection.FULL:
        findings.append(Finding(Reason.STOP_COVERAGE_INCOMPLETE, detail))
        return None
    stop = _parse(stop_price, detail=f"{detail}.stop_price", mode="positive", findings=findings)
    covered_qty = _parse(covered, detail=f"{detail}.covered_quantity", mode="positive", findings=findings)
    if stop is None or covered_qty is None or quantity is None:
        return None
    if covered_qty != quantity:
        findings.append(Finding(Reason.STOP_COVERAGE_INCOMPLETE, detail))
        return None
    if entry is not None and stop >= entry:
        findings.append(Finding(Reason.STOP_NOT_BELOW_ENTRY, detail))
        return None
    return stop


def _gbp_fx(
    currency: str,
    fx: Decimal | None,
    conservative_fx: Decimal | None,
    findings: list[Finding],
    detail: str,
) -> None:
    if _blank(currency):
        findings.append(Finding(Reason.CURRENCY_REJECTED, detail))
        return
    if currency != "GBP":
        return
    if fx is not None and fx != _ONE:
        findings.append(Finding(Reason.FX_NOT_ONE_FOR_GBP, f"{detail}.fx"))
    if conservative_fx is not None and conservative_fx != _ONE:
        findings.append(Finding(Reason.FX_NOT_ONE_FOR_GBP, f"{detail}.conservative_fx"))


def _quantity(value: object, precision: object, findings: list[Finding], detail: str) -> Decimal | None:
    if not _precision_ok(precision):
        findings.append(Finding(Reason.PRECISION_INVALID, detail))
        return None
    assert isinstance(precision, int)
    return _parse(value, detail=detail, mode="positive", places=precision, findings=findings)


def _precision_ok(precision: object) -> bool:
    return isinstance(precision, int) and not isinstance(precision, bool) and 0 <= precision <= 8


def _operational_findings(
    *,
    clock: ClockInput,
    quote: QuoteInput | None,
    fx_quote: FxQuoteInput | None,
    session: SessionInput | None,
    fixture_limits: FixtureOperationalLimits | None,
) -> list[Finding]:
    findings: list[Finding] = [Finding(Reason.ALLOWLIST_EMPTY, "instrument_allowlist")]
    if fixture_limits is None:
        findings.append(Finding(Reason.SETTINGS_UNAPPROVED_UNCONFIGURED, "operational_numerical_settings"))
        findings.append(Finding(Reason.SESSION_UNAPPROVED_UNCONFIGURED, "session_calendar"))
    else:
        findings.append(Finding(Reason.FIXTURE_NOT_OWNER_APPROVED, "fixture_limits"))
    _clock(clock, findings)
    _quote(quote, clock, fixture_limits, findings)
    _fx(fx_quote, clock, fixture_limits, findings)
    _session(session, fixture_limits, findings)
    return findings


def _clock(clock: ClockInput, findings: list[Finding]) -> None:
    if not isinstance(clock.as_of, datetime):
        findings.append(Finding(Reason.CLOCK_MISSING, "as_of"))
        return
    if not _aware(clock.as_of):
        findings.append(Finding(Reason.CLOCK_NAIVE, "as_of"))
        return
    if clock.previous_as_of is None:
        return
    if not isinstance(clock.previous_as_of, datetime) or not _aware(clock.previous_as_of):
        findings.append(Finding(Reason.CLOCK_NAIVE, "previous_as_of"))
        return
    if clock.as_of < clock.previous_as_of:
        findings.append(Finding(Reason.CLOCK_ROLLBACK, "as_of"))


def _quote(
    quote: QuoteInput | None,
    clock: ClockInput,
    limits: FixtureOperationalLimits | None,
    findings: list[Finding],
) -> None:
    if quote is None:
        findings.append(Finding(Reason.QUOTE_MISSING, "quote"))
        return
    _positive_quote_price(quote.bid, "quote.bid", Reason.QUOTE_NOT_POSITIVE, findings)
    _positive_quote_price(quote.ask, "quote.ask", Reason.QUOTE_NOT_POSITIVE, findings)
    observed = _timestamp(
        quote.observed_at,
        "quote.observed_at",
        clock,
        findings,
        naive=Reason.QUOTE_TIMESTAMP_NAIVE,
        future=Reason.QUOTE_TIMESTAMP_FUTURE,
    )
    if _blank(quote.source):
        findings.append(Finding(Reason.QUOTE_SOURCE_MISSING, "quote.source"))
    if _blank(quote.quality):
        findings.append(Finding(Reason.QUOTE_QUALITY_MISSING, "quote.quality"))
    if limits is None or observed is None or not isinstance(clock.as_of, datetime) or not _aware(clock.as_of):
        return
    if clock.as_of - observed > timedelta(seconds=limits.quote_max_age_seconds):
        findings.append(Finding(Reason.QUOTE_STALE, "quote"))
    if not _blank(quote.source) and quote.source not in limits.quote_sources:
        findings.append(Finding(Reason.QUOTE_SOURCE_REJECTED, "quote.source"))
    if not _blank(quote.quality) and quote.quality not in limits.quality_labels:
        findings.append(Finding(Reason.QUOTE_QUALITY_REJECTED, "quote.quality"))


def _fx(
    quote: FxQuoteInput | None,
    clock: ClockInput,
    limits: FixtureOperationalLimits | None,
    findings: list[Finding],
) -> None:
    if quote is None:
        findings.append(Finding(Reason.FX_QUOTE_MISSING, "fx_quote"))
        return
    _positive_quote_price(quote.rate_to_gbp, "fx_quote.rate", Reason.FX_NOT_POSITIVE, findings)
    observed = _timestamp(
        quote.observed_at,
        "fx_quote.observed_at",
        clock,
        findings,
        naive=Reason.FX_TIMESTAMP_NAIVE,
        future=Reason.FX_TIMESTAMP_FUTURE,
    )
    if _blank(quote.source):
        findings.append(Finding(Reason.FX_SOURCE_MISSING, "fx_quote.source"))
    if limits is None or observed is None or not isinstance(clock.as_of, datetime) or not _aware(clock.as_of):
        return
    if clock.as_of - observed > timedelta(seconds=limits.fx_max_age_seconds):
        findings.append(Finding(Reason.FX_STALE, "fx_quote"))
    if not _blank(quote.source) and quote.source not in limits.fx_sources:
        findings.append(Finding(Reason.FX_SOURCE_REJECTED, "fx_quote.source"))


def _session(
    session: SessionInput | None,
    limits: FixtureOperationalLimits | None,
    findings: list[Finding],
) -> None:
    if session is None or _blank(session.calendar_id) or _blank(session.session_id):
        findings.append(Finding(Reason.SESSION_OR_CALENDAR_MISSING, "session"))
        if limits is not None:
            findings.append(Finding(Reason.SESSION_NOT_CONFIRMED, "session"))
        return
    if limits is None:
        return
    if session.calendar_id != limits.calendar_id:
        findings.append(Finding(Reason.CALENDAR_REJECTED, "calendar_id"))
    if session.session_id != limits.session_id:
        findings.append(Finding(Reason.SESSION_REJECTED, "session_id"))
    if session.in_session is not True:
        findings.append(Finding(Reason.SESSION_NOT_CONFIRMED, "in_session"))


def _positive_quote_price(value: object, detail: str, negative_reason: Reason, findings: list[Finding]) -> None:
    parsed = _parse(value, detail=detail, mode="positive", findings=findings)
    if parsed is None and not any(
        item.detail == detail
        and item.reason in {Reason.FLOAT_REJECTED, Reason.NONFINITE, Reason.MISSING, Reason.INVALID_DECIMAL, Reason.SCALE_EXCEEDED}
        for item in findings
    ):
        findings.append(Finding(negative_reason, detail))


def _timestamp(
    value: datetime | None,
    detail: str,
    clock: ClockInput,
    findings: list[Finding],
    *,
    naive: Reason,
    future: Reason,
) -> datetime | None:
    """Return an aware timestamp that is not after the clock, or None when it cannot be aged."""
    if value is None:
        findings.append(Finding(Reason.MISSING, detail))
        return None
    if not isinstance(value, datetime) or not _aware(value):
        findings.append(Finding(naive, detail))
        return None
    if isinstance(clock.as_of, datetime) and _aware(clock.as_of) and value > clock.as_of:
        findings.append(Finding(future, detail))
        return None
    return value


def _parse(
    value: object,
    *,
    detail: str,
    mode: str,
    findings: list[Finding],
    places: int = 8,
) -> Decimal | None:
    if value is None:
        findings.append(Finding(Reason.MISSING, detail))
        return None
    if isinstance(value, float):
        findings.append(Finding(Reason.FLOAT_REJECTED, detail))
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, str)):
        findings.append(Finding(Reason.INVALID_DECIMAL, detail))
        return None
    if isinstance(value, str) and "e" in value.lower():
        findings.append(Finding(Reason.INVALID_DECIMAL, detail))
        return None
    if isinstance(value, Decimal) and not value.is_finite():
        findings.append(Finding(Reason.NONFINITE, detail))
        return None
    try:
        if mode == "penny_signed":
            amount = signed_money(value, label=detail)
        elif mode == "penny_non_negative":
            amount = require_money(value, label=detail)
        else:
            amount = require_scale(value, label=detail, places=places)
    except MoneyError as exc:
        text = str(exc)
        if "finite" in text:
            findings.append(Finding(Reason.NONFINITE, detail))
        elif "decimal places" in text:
            findings.append(Finding(Reason.SCALE_EXCEEDED, detail))
        else:
            findings.append(Finding(Reason.INVALID_DECIMAL, detail))
        return None
    if mode in {"positive", "penny_non_negative"} and amount < 0:
        findings.append(Finding(Reason.NEGATIVE_REJECTED, detail))
        return None
    if mode == "positive" and amount <= 0:
        findings.append(Finding(Reason.NOT_POSITIVE, detail))
        return None
    if mode == "non_negative" and amount < 0:
        findings.append(Finding(Reason.NEGATIVE_REJECTED, detail))
        return None
    if mode == "penny_non_negative" and amount == 0:
        return amount
    return amount


def _active(states: set[PositionStatus]) -> bool:
    return PositionStatus.OPEN in states or PositionStatus.PENDING_OPEN in states


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _blank(value: object) -> bool:
    return not isinstance(value, str) or value.strip() == ""


def _enum_detail(value: object) -> str:
    text = getattr(value, "value", None)
    if isinstance(text, str):
        return text
    return str(value)

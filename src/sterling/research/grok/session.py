"""Log a snapshot, call a provider, and keep only a screened paper decision.

A late, malformed, or backdated model reply is stored as HOLD at the receipt
time. The receipt time is never rewritten to an earlier stamp. Execution
permission cannot be granted by the provider or by the screen.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ForwardProposal, ProposalLog, prompt_hash, require_utc, snapshot_hash
from sterling.research.grok.safety import DAILY_LOSS_CAP, PER_TRADE_CAP, pinned_review
from sterling.research.grok.schema import (
    AccountView,
    MarketSnapshot,
    ParseFailure,
    ParseOk,
    account_payload,
    as_mapping,
    instrument_payload,
    parse_response,
    prompt_for,
    response_hash,
)
from sterling.research.isolation import EXECUTION_PERMISSION, ORDERS_SUBMITTED, assert_not_on_execution_path
from sterling.research.money import ZERO, parse_decimal

Screen = Callable[[Mapping[str, object]], Mapping[str, object]]
MODEL_VERSION = "grok-decision-v1"


class Completer(Protocol):
    def complete(self, snapshot: MarketSnapshot, *, prompt: str) -> object: ...


_CAP_TRADE = format(PER_TRADE_CAP, "f")
_CAP_DAY = format(DAILY_LOSS_CAP, "f")


@dataclass(frozen=True, slots=True)
class ForwardDecision:
    accepted_for_paper: bool
    weights: dict[str, Decimal]
    reasons: tuple[str, ...]
    execution_permission: str
    orders_submitted: int
    allowlist_permitted: bool
    per_trade_cap_gbp: str
    daily_loss_cap_gbp: str
    proposal_logged_at: datetime
    screen_logged_at: datetime
    inputs_snapshot_hash: str
    prompt_hash: str
    response_hash: str
    model_id: str
    confidence: Decimal | None


def _proposal_text(
    weights: Mapping[str, Decimal],
    *,
    action: str,
    response: str,
    reasons: Sequence[str],
    notes: str,
) -> str:
    return json.dumps(
        {
            "action": action,
            "book": "grok",
            "notes": notes[:300],
            "reasons": list(reasons),
            "response_hash": response,
            "weights": {ticker: format(weights[ticker], "f") for ticker in sorted(weights)},
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _clean(review: Mapping[str, object], fallback: str) -> tuple[bool, dict[str, Decimal], tuple[str, ...]]:
    permission = review.get("execution_permission")
    orders = review.get("orders_submitted")
    allow = review.get("allowlist_permitted")
    caps_ok = review.get("per_trade_cap_gbp") == _CAP_TRADE and review.get("daily_loss_cap_gbp") == _CAP_DAY
    granted = permission != EXECUTION_PERMISSION or orders != ORDERS_SUBMITTED or allow is not False or not caps_ok
    reasons_raw = review.get("reasons")
    reasons = [str(item) for item in reasons_raw] if isinstance(reasons_raw, list) else [fallback]
    weights: dict[str, Decimal] = {}
    raw_weights = review.get("weights")
    bad_weights = False
    if isinstance(raw_weights, dict):
        total = ZERO
        for key, value in raw_weights.items():
            if not isinstance(key, str):
                bad_weights = True
                break
            try:
                weight = parse_decimal(value, label=key)
            except ResearchError:
                bad_weights = True
                break
            if weight < 0 or weight > 1:
                bad_weights = True
                break
            if weight > 0:
                weights[key] = weight
                total += weight
        if total > 1:
            bad_weights = True
    elif raw_weights not in (None, {}):
        bad_weights = True
    accepted = review.get("accepted_for_paper") is True and review.get("budget_blocked") is not True
    if granted or bad_weights:
        reasons = ["screen_rejected", *reasons]
        accepted = False
    if not accepted:
        weights = {}
    if not reasons:
        reasons = [fallback]
    return accepted, weights, tuple(reasons)


class ForwardSession:
    def __init__(
        self,
        log: ProposalLog,
        provider: Completer,
        *,
        screen: Screen | None = None,
        response_timeout: timedelta = timedelta(seconds=60),
    ) -> None:
        self.log = log
        self._provider: Completer = provider
        self._screen: Screen = screen if screen is not None else pinned_review
        if response_timeout <= timedelta(0):
            raise ResearchError("response timeout must be positive")
        self.response_timeout = response_timeout

    def decide(
        self,
        snapshot: MarketSnapshot,
        *,
        received_at: datetime,
        account: AccountView,
        paper_nav_gbp: Decimal,
    ) -> ForwardDecision:
        assert_not_on_execution_path()
        receipt = require_utc(received_at)
        if receipt <= snapshot.as_of:
            raise ResearchError("response receipt is not after the snapshot")
        prices = {quote.instrument_id: ((quote.observed_at, quote.price),) for quote in snapshot.quotes}
        digest = snapshot_hash(prices)
        prompt = prompt_for(snapshot)
        prompt_digest = prompt_hash(prompt)
        self._snapshot(snapshot, digest, prices)
        price_times = tuple(quote.observed_at for quote in snapshot.quotes)
        deadline = snapshot.as_of + self.response_timeout
        if receipt > deadline:
            return self._hold(
                snapshot,
                account=account,
                paper_nav_gbp=paper_nav_gbp,
                receipt=receipt,
                digest=digest,
                prompt_digest=prompt_digest,
                response_digest=prompt_hash("late"),
                model_id="unanswered",
                reason="late_response",
                price_times=price_times,
            )
        try:
            envelope = self._provider.complete(snapshot, prompt=prompt)
        except Exception as exc:
            return self._hold(
                snapshot,
                account=account,
                paper_nav_gbp=paper_nav_gbp,
                receipt=receipt,
                digest=digest,
                prompt_digest=prompt_digest,
                response_digest=prompt_hash(type(exc).__name__),
                model_id="provider-failed",
                reason=f"provider_failed:{type(exc).__name__}",
                price_times=price_times,
            )
        try:
            model_id, responded_at, raw = _envelope(envelope)
        except ResearchError:
            return self._hold(
                snapshot,
                account=account,
                paper_nav_gbp=paper_nav_gbp,
                receipt=receipt,
                digest=digest,
                prompt_digest=prompt_digest,
                response_digest=prompt_hash("malformed"),
                model_id="malformed",
                reason="malformed_json",
                price_times=price_times,
            )
        response_digest = response_hash(raw)
        if responded_at < snapshot.as_of:
            return self._hold(
                snapshot,
                account=account,
                paper_nav_gbp=paper_nav_gbp,
                receipt=receipt,
                digest=digest,
                prompt_digest=prompt_digest,
                response_digest=response_digest,
                model_id=model_id,
                reason="backdated_response",
                price_times=price_times,
            )
        if responded_at > receipt or responded_at > deadline:
            return self._hold(
                snapshot,
                account=account,
                paper_nav_gbp=paper_nav_gbp,
                receipt=receipt,
                digest=digest,
                prompt_digest=prompt_digest,
                response_digest=response_digest,
                model_id=model_id,
                reason="late_response",
                price_times=price_times,
            )
        parsed = parse_response(raw, prompt_digest_value=prompt_digest, response_digest=response_digest)
        if isinstance(parsed, ParseFailure) or parsed.model_id != model_id:
            reason = parsed.reason if isinstance(parsed, ParseFailure) else "malformed_json"
            return self._hold(
                snapshot,
                account=account,
                paper_nav_gbp=paper_nav_gbp,
                receipt=receipt,
                digest=digest,
                prompt_digest=prompt_digest,
                response_digest=response_digest,
                model_id=model_id,
                reason=reason,
                price_times=price_times,
            )
        return self._accept_parse(
            snapshot,
            parsed,
            account=account,
            paper_nav_gbp=paper_nav_gbp,
            receipt=receipt,
            digest=digest,
            price_times=price_times,
        )

    def _snapshot(
        self,
        snapshot: MarketSnapshot,
        digest: str,
        prices: Mapping[str, tuple[tuple[datetime, Decimal], ...]],
    ) -> None:
        self.log.append_event(
            timestamp_utc=snapshot.as_of,
            record_type="snapshot",
            fields={
                "inputs_snapshot_hash": digest,
                "nav_gbp": format(snapshot.nav_gbp, "f"),
                "positions": [
                    {
                        "instrument_id": position.instrument_id,
                        "market_value_gbp": format(position.market_value_gbp, "f"),
                        "quantity": format(position.quantity, "f"),
                    }
                    for position in sorted(snapshot.positions, key=lambda item: item.instrument_id)
                ],
                "quotes": [
                    {
                        "instrument_id": code,
                        "observed_at": observed.isoformat(),
                        "price": format(price, "f"),
                    }
                    for code, series in sorted(prices.items())
                    for observed, price in series
                ],
            },
            price_timestamps=tuple(quote.observed_at for quote in snapshot.quotes),
        )

    def _hold(
        self,
        snapshot: MarketSnapshot,
        *,
        account: AccountView,
        paper_nav_gbp: Decimal,
        receipt: datetime,
        digest: str,
        prompt_digest: str,
        response_digest: str,
        model_id: str,
        reason: str,
        price_times: tuple[datetime, ...],
    ) -> ForwardDecision:
        review = self._review(
            snapshot,
            account=account,
            paper_nav_gbp=paper_nav_gbp,
            instruments=[],
            rejection_reason=reason,
        )
        _accepted, _weights, reasons = _clean(review, reason)
        if reason not in reasons:
            reasons = (reason, *reasons)
        self._log_proposal(
            when=receipt,
            model_id=model_id,
            prompt_digest=prompt_digest,
            digest=digest,
            weights={},
            action="HOLD",
            response_digest=response_digest,
            reasons=reasons,
            notes=reason,
            price_times=price_times,
        )
        screened = receipt + timedelta(microseconds=1)
        self._log_screen(screened, accepted=False, weights={}, reasons=reasons, price_times=price_times)
        return _decision(
            accepted=False,
            weights={},
            reasons=reasons,
            proposal_logged_at=receipt,
            screen_logged_at=screened,
            digest=digest,
            prompt_digest=prompt_digest,
            response_digest=response_digest,
            model_id=model_id,
            confidence=None,
        )

    def _accept_parse(
        self,
        snapshot: MarketSnapshot,
        parsed: ParseOk,
        *,
        account: AccountView,
        paper_nav_gbp: Decimal,
        receipt: datetime,
        digest: str,
        price_times: tuple[datetime, ...],
    ) -> ForwardDecision:
        instruments = [instrument_payload(item) for item in parsed.instruments]
        review = self._review(
            snapshot,
            account=account,
            paper_nav_gbp=paper_nav_gbp,
            instruments=instruments,
            rejection_reason=None,
        )
        accepted, weights, reasons = _clean(review, "screen_rejected")
        action = "HOLD" if not weights else "REBALANCE"
        confidence = sum((item.confidence for item in parsed.instruments), ZERO) / Decimal(len(parsed.instruments))
        self._log_proposal(
            when=receipt,
            model_id=parsed.model_id,
            prompt_digest=parsed.instruments[0].prompt_hash,
            digest=digest,
            weights=weights,
            action=action,
            response_digest=parsed.instruments[0].response_hash,
            reasons=reasons,
            notes=" | ".join(item.reasoning for item in parsed.instruments),
            price_times=price_times,
        )
        screened = receipt + timedelta(microseconds=1)
        self._log_screen(screened, accepted=accepted, weights=weights, reasons=reasons, price_times=price_times)
        return _decision(
            accepted=accepted,
            weights=weights if accepted else {},
            reasons=reasons,
            proposal_logged_at=receipt,
            screen_logged_at=screened,
            digest=digest,
            prompt_digest=parsed.instruments[0].prompt_hash,
            response_digest=parsed.instruments[0].response_hash,
            model_id=parsed.model_id,
            confidence=confidence if accepted else None,
        )

    def _review(
        self,
        snapshot: MarketSnapshot,
        *,
        account: AccountView,
        paper_nav_gbp: Decimal,
        instruments: list[dict[str, object]],
        rejection_reason: str | None,
    ) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "account": account_payload(account),
            "as_of": snapshot.as_of.isoformat(),
            "instruments": instruments,
            "paper_nav_gbp": format(paper_nav_gbp, "f"),
            "positions": [
                {
                    "instrument_id": position.instrument_id,
                    "market_value_gbp": format(position.market_value_gbp, "f"),
                    "quantity": format(position.quantity, "f"),
                }
                for position in snapshot.positions
            ],
            "quoted": [quote.instrument_id for quote in snapshot.quotes],
            "quotes": [{"instrument_id": quote.instrument_id, "price": format(quote.price, "f")} for quote in snapshot.quotes],
        }
        if rejection_reason is not None:
            payload["rejection_reason"] = rejection_reason
        try:
            review = self._screen(payload)
        except Exception as exc:
            return _forced(f"screen_failed:{type(exc).__name__}")
        if not isinstance(review, Mapping):
            return _forced("screen_rejected")
        return as_mapping(review)

    def _log_proposal(
        self,
        *,
        when: datetime,
        model_id: str,
        prompt_digest: str,
        digest: str,
        weights: Mapping[str, Decimal],
        action: str,
        response_digest: str,
        reasons: Sequence[str],
        notes: str,
        price_times: Sequence[datetime],
    ) -> None:
        proposal = ForwardProposal(
            timestamp_utc=when,
            model=model_id,
            model_version=MODEL_VERSION,
            prompt_hash=prompt_digest,
            inputs_snapshot_hash=digest,
            proposal=_proposal_text(weights, action=action, response=response_digest, reasons=reasons, notes=notes),
            rationale="; ".join(reasons)[:500],
        )
        self.log.append_proposal(proposal, price_timestamps=price_times)

    def _log_screen(
        self,
        when: datetime,
        *,
        accepted: bool,
        weights: Mapping[str, Decimal],
        reasons: Sequence[str],
        price_times: Sequence[datetime],
    ) -> None:
        self.log.append_event(
            timestamp_utc=when,
            record_type="screen",
            fields={
                "accepted_for_paper": "true" if accepted else "false",
                "allowlist_permitted": "false",
                "daily_loss_cap_gbp": _CAP_DAY,
                "execution_permission": EXECUTION_PERMISSION,
                "orders_submitted": ORDERS_SUBMITTED,
                "per_trade_cap_gbp": _CAP_TRADE,
                "reasons": list(reasons),
                "weights": {ticker: format(weights[ticker], "f") for ticker in sorted(weights)},
            },
            price_timestamps=price_times,
        )


def _envelope(value: object) -> tuple[str, datetime, str]:
    model_id = getattr(value, "model_id", None)
    responded_at = getattr(value, "responded_at", None)
    raw = getattr(value, "raw_response", None)
    if not isinstance(model_id, str) or not isinstance(responded_at, datetime) or not isinstance(raw, str):
        raise ResearchError("provider response is not an envelope")
    return model_id, require_utc(responded_at), raw


def _forced(reason: str) -> dict[str, object]:
    return {
        "accepted_for_paper": False,
        "allowlist_permitted": False,
        "budget_blocked": True,
        "daily_loss_cap_gbp": _CAP_DAY,
        "execution_permission": EXECUTION_PERMISSION,
        "orders_submitted": ORDERS_SUBMITTED,
        "per_trade_cap_gbp": _CAP_TRADE,
        "reasons": [reason],
        "weights": {},
    }


def _decision(
    *,
    accepted: bool,
    weights: dict[str, Decimal],
    reasons: tuple[str, ...],
    proposal_logged_at: datetime,
    screen_logged_at: datetime,
    digest: str,
    prompt_digest: str,
    response_digest: str,
    model_id: str,
    confidence: Decimal | None,
) -> ForwardDecision:
    return ForwardDecision(
        accepted_for_paper=accepted,
        weights=weights if accepted else {},
        reasons=reasons,
        execution_permission=EXECUTION_PERMISSION,
        orders_submitted=ORDERS_SUBMITTED,
        allowlist_permitted=False,
        per_trade_cap_gbp=_CAP_TRADE,
        daily_loss_cap_gbp=_CAP_DAY,
        proposal_logged_at=proposal_logged_at,
        screen_logged_at=screen_logged_at,
        inputs_snapshot_hash=digest,
        prompt_hash=prompt_digest,
        response_hash=response_digest,
        model_id=model_id,
        confidence=confidence,
    )

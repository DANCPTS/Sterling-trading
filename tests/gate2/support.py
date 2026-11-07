"""Fixture builders for Gate 2. Synthetic instruments only."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tests.risk_support import fixture_limits, proposal
from tests.support import june, settle

from sterling.gate2.admit import admit
from sterling.gate2.ingress import ingest
from sterling.gate2.models import (
    AdmissionRequest,
    DecisionResult,
    FxEvidence,
    GenerationInput,
    HoldingQuote,
    StopEvidence,
    TrustedClock,
)
from sterling.gate2.store import Gate2Store, provision_store
from sterling.ledger.events import MarkEvent
from sterling.ledger.ledger import Ledger
from sterling.risk.contracts import EntryProposal
from sterling.risk.policy import Stage2Policy, load_default_policy
from sterling.rules.loader import load_default_rules

MOMENT = june(1, 10)
_UNSET = object()


def clock(moment: datetime = MOMENT, *, monotonic_ns: int = 1) -> TrustedClock:
    return TrustedClock(as_of=moment, monotonic_ns=monotonic_ns)


def policy() -> Stage2Policy:
    return load_default_policy()


def new_store(
    root: Path,
    *,
    fixture_scope: bool = True,
    separated: bool = False,
    owner_uid: int | None = None,
    ingress_uid: int | None = None,
    ai_uid: int | None = None,
    risk_uid: int | None = None,
) -> Gate2Store:
    uid = os.getuid()
    return provision_store(
        root / "journal",
        account_id="sim-primary",
        calendar_version="fixture-cal-v1",
        risk_uid=uid if risk_uid is None else risk_uid,
        owner_uid=(uid + 1) if owner_uid is None else owner_uid,
        ingress_uid=uid if ingress_uid is None else ingress_uid,
        ai_uid=(uid + 2) if ai_uid is None else ai_uid,
        os_identity_separated=separated,
        synthetic_fixture_scope=fixture_scope,
        created_at="2026-06-01T08:00:00+00:00",
    )


def fresh_book(path: Path) -> Ledger:
    ledger = Ledger.open_primary_account(load_default_rules())
    ledger.save(path)
    return ledger


def quote_for(price: str, moment: datetime = MOMENT) -> HoldingQuote:
    amount = Decimal(price)
    return HoldingQuote(
        bid=amount,
        ask=amount,
        mark=amount,
        observed_at=moment - timedelta(seconds=5),
        source="FIXTURE-SOURCE",
        quality="FIXTURE-QUALITY",
    )


def generation(
    path: Path,
    sequence: int,
    *,
    moment: datetime = MOMENT,
    opening: bool = False,
    quotes: dict[str, HoldingQuote] | None = None,
    stops: dict[str, StopEvidence] | None = None,
    pending_stops: dict[str, StopEvidence] | None = None,
    limits: object = _UNSET,
    verified: bool = True,
    costs_final: bool = True,
    ambiguous: tuple[str, ...] = (),
    reported_equity: str | None = None,
    reported_cash: str | None = None,
    opening_claim: str | None = None,
    calendar_version: str = "fixture-cal-v1",
) -> GenerationInput:
    use_limits = fixture_limits(86_400) if limits is _UNSET else limits
    return GenerationInput(
        source_id="fixture",
        source_sequence=sequence,
        ledger_path=str(path),
        as_of=moment,
        receipt_time=moment,
        calendar_id="FIXTURE-CAL",
        calendar_version=calendar_version,
        session_id="FIXTURE-SESSION",
        in_session=True,
        verified_trading_day=verified,
        is_opening_snapshot=opening,
        opening_claim=opening_claim or ("FIRST_FRESH_OPENING" if opening else "NONE"),
        quotes=quotes if quotes is not None else {"SYNTH-EQ": quote_for("10.00", moment)},
        fx={"GBP": FxEvidence(Decimal("1"), moment, "FIXTURE-FX")},
        holding_stops=stops or {},
        pending_stops=pending_stops or {},
        reported_cash_gbp=None if reported_cash is None else Decimal(reported_cash),
        reported_equity_gbp=None if reported_equity is None else Decimal(reported_equity),
        reported_exposure_gbp=None,
        costs_final=costs_final,
        ambiguous_fill_ids=ambiguous,
        fixture_limits=use_limits,  # type: ignore[arg-type]
    )


def anchor(store: Gate2Store, ledger: Ledger, path: Path, sequence: int = 1, **kwargs: object) -> DecisionResult:
    if sequence == 1 and "opening" not in kwargs:
        kwargs["opening"] = True
    ledger.save(path)
    moment = kwargs.get("moment", MOMENT)
    assert isinstance(moment, datetime)
    return ingest(
        store,
        generation(path, sequence, **kwargs),  # type: ignore[arg-type]
        clock(moment),
        peer_uid=store.identity.ingress_uid,
    )


def entry(**overrides: object) -> EntryProposal:
    return proposal(**overrides)


def request(name: str = "r1", **overrides: object) -> AdmissionRequest:
    return AdmissionRequest(
        request_id=name,
        principal="strategy",
        account_id="sim-primary",
        environment="simulated",
        proposal=entry(**overrides),
    )


def admit_ok(store: Gate2Store, name: str = "r1", **overrides: object) -> DecisionResult:
    return admit(store, request(name, **overrides), clock())


def buy_marked(ledger: Ledger, *, instrument: str, price: str, mark: str, quantity: str = "1", moment: datetime = MOMENT) -> None:
    settle(
        ledger,
        event_id=f"buy-{instrument}-{price}",
        timestamp=moment,
        instrument_id=instrument,
        side="buy",
        quantity=quantity,
        price=price,
    )
    ledger.post(MarkEvent(f"mark-{instrument}-{mark}", moment, instrument, Decimal(mark), Decimal("1"), source="fixture"))

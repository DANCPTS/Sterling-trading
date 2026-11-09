"""Draft candidate universe and the runtime instrument record.

The JSON file is DRAFT and not frozen. It is not an operational allowlist.
Fee lookup is disclosure. It does not change simulated NAV.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib.resources import files

from sterling.research.errors import ResearchError, UnverifiedInput
from sterling.research.money import ZERO, parse_decimal

OPERATIONAL_VERSION_IDS = frozenset({"desk-rules-v0.2", "stage2-gate1-policy-v1"})


@dataclass(frozen=True, slots=True)
class Instrument:
    ticker: str
    instrument_type: str
    product_class: str
    asset_class: str
    trading_currency: str
    stamp_duty_exempt: bool
    distribution: str
    calendar_id: str
    isin: str | None = None


@dataclass(frozen=True, slots=True)
class FeeObservation:
    effective_from: date | None
    effective_until: date | None
    annual_rate: Decimal | None
    status: str
    safe_to_apply_historically: bool
    note: str
    fee_label: str


@dataclass(frozen=True, slots=True)
class FeeQuote:
    annual_rate: Decimal | None
    status: str
    safe_to_apply_historically: bool
    note: str


@dataclass(frozen=True, slots=True)
class Candidate:
    ticker: str
    name: str
    isin: str
    draft_role: str
    in_provisional_starting_set: bool
    product_class: str
    ucits_fund: bool
    instrument_type: str
    asset_class: str
    distribution: str
    yahoo_quote_currency: str
    price_divisor_to_gbp: Decimal
    stooq_history_start: str
    lse_listing_date: str
    fees: tuple[FeeObservation, ...]
    raw: dict[str, object]

    def as_instrument(self) -> Instrument:
        return Instrument(
            ticker=self.ticker,
            instrument_type=self.instrument_type,
            product_class=self.product_class,
            asset_class=self.asset_class,
            trading_currency="GBP",
            stamp_duty_exempt=True,
            distribution=self.distribution,
            calendar_id="XLON_WEEKDAY",
            isin=self.isin,
        )

    def fee_on(self, day: date) -> FeeQuote:
        matching: list[FeeObservation] = []
        for observation in self.fees:
            if observation.effective_until is not None and day > observation.effective_until:
                continue
            if observation.effective_from is not None and day < observation.effective_from:
                continue
            matching.append(observation)
        if not matching:
            raise ResearchError(f"{self.ticker} has no fee observation for {day.isoformat()}")
        chosen = max(matching, key=lambda observation: observation.effective_from or date.min)
        return FeeQuote(
            annual_rate=chosen.annual_rate,
            status=chosen.status,
            safe_to_apply_historically=chosen.safe_to_apply_historically,
            note=chosen.note,
        )


@dataclass(frozen=True, slots=True)
class CandidateUniverse:
    status: str
    frozen: bool
    provisional_starting_set: tuple[str, ...]
    optional_comparison_only: tuple[str, ...]
    fscs_general_information: str
    instruments: tuple[Candidate, ...]

    def by_ticker(self, ticker: str) -> Candidate:
        for instrument in self.instruments:
            if instrument.ticker == ticker:
                return instrument
        raise ResearchError(f"{ticker} is not in the draft universe")


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ResearchError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _text(document: dict[str, object], key: str) -> str:
    value = document[key]
    if not isinstance(value, str) or not value:
        raise ResearchError(f"{key} must be a non-empty string")
    return value


def _optional_date(value: object, label: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResearchError(f"{label} must be a date or null")
    if value == "UNVERIFIED":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise UnverifiedInput(f"{label} is not a verified date: {value}") from exc


def _fee(raw: object) -> FeeObservation:
    document = _object(raw, "fee")
    rate_text = document.get("annual_rate")
    rate = None if rate_text is None else parse_decimal(rate_text, label="annual_rate")
    if rate is not None and rate < ZERO:
        raise ResearchError("a fee rate cannot be negative")
    label = document.get("fee_label", "OCF")
    if not isinstance(label, str):
        raise ResearchError("fee_label must be a string")
    return FeeObservation(
        effective_from=_optional_date(document.get("effective_from"), "effective_from"),
        effective_until=_optional_date(document.get("effective_until"), "effective_until"),
        annual_rate=rate,
        status=_text(document, "status"),
        safe_to_apply_historically=document.get("safe_to_apply_historically") is True,
        note=_text(document, "note"),
        fee_label=label,
    )


def _candidate(raw: object) -> Candidate:
    document = _object(raw, "instrument")
    fees_raw = document.get("fees")
    if not isinstance(fees_raw, list) or not fees_raw:
        raise ResearchError(f"{document.get('ticker')} needs a fee list")
    return Candidate(
        ticker=_text(document, "ticker"),
        name=_text(document, "name"),
        isin=_text(document, "isin"),
        draft_role=_text(document, "draft_role"),
        in_provisional_starting_set=document.get("in_provisional_starting_set") is True,
        product_class=_text(document, "product_class"),
        ucits_fund=document.get("ucits_fund") is True,
        instrument_type=_text(document, "instrument_type"),
        asset_class=_text(document, "asset_class"),
        distribution=_text(document, "distribution"),
        yahoo_quote_currency=_text(document, "yahoo_quote_currency"),
        price_divisor_to_gbp=parse_decimal(document.get("price_divisor_to_gbp"), label="price_divisor_to_gbp"),
        stooq_history_start=_text(document, "stooq_history_start"),
        lse_listing_date=_text(document, "lse_listing_date"),
        fees=tuple(_fee(item) for item in fees_raw),
        raw=document,
    )


def parse_candidate_universe(document: dict[str, object]) -> CandidateUniverse:
    if document.get("version_id") in OPERATIONAL_VERSION_IDS:
        raise ResearchError("refusing to load an operational policy as the research universe")
    if document.get("status") != "DRAFT" or document.get("frozen") is not False:
        raise ResearchError("candidate universe must stay DRAFT and not frozen")
    raw_instruments = document.get("instruments")
    if not isinstance(raw_instruments, list):
        raise ResearchError("instruments must be a list")
    instruments = tuple(_candidate(item) for item in raw_instruments)
    starting = document.get("provisional_starting_set")
    optional = document.get("optional_comparison_only")
    if not isinstance(starting, list) or not isinstance(optional, list):
        raise ResearchError("starting set and optional list must be arrays")
    return CandidateUniverse(
        status="DRAFT",
        frozen=False,
        provisional_starting_set=tuple(str(item) for item in starting),
        optional_comparison_only=tuple(str(item) for item in optional),
        fscs_general_information=_text(document, "fscs_general_information"),
        instruments=instruments,
    )


def load_candidate_universe() -> CandidateUniverse:
    resource = files("sterling.research").joinpath("data/candidate-universe-draft.json")
    loaded = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ResearchError("candidate universe must be a JSON object")
    return parse_candidate_universe({str(key): value for key, value in loaded.items()})


def gross_index_fee_drag(nav_gbp: Decimal, annual_rate: Decimal) -> Decimal:
    """One calendar day of fee drag on a gross index level.

    Do not call this on an exchange price. Those prices are already net of the
    fund or issuer charge.
    """
    from sterling.research.money import quantize_money

    if nav_gbp < 0 or annual_rate < 0:
        raise ResearchError("fee drag inputs cannot be negative")
    return quantize_money(nav_gbp * annual_rate / Decimal("365.25"))

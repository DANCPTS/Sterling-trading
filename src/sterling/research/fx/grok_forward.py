"""Append-only forward log for proposals that must be frozen before the outcome.

The log is created empty of proposals. This module does not call a model, does
not open a socket, and does not manufacture a historical track record. A
proposal timestamped before the log was created, or not strictly after the
previous record, is rejected. Snapshots, screen results, and the
pre-registration share that chain. The scorer reads only prices strictly after
the proposal timestamp.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from sterling.research.errors import ResearchError, ResearchIsolationError

SCHEMA = "fx-forward-log-v1"
GENESIS = "0" * 64


def require_utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ResearchError("proposal timestamp must be UTC")
    return timestamp.astimezone(UTC)


def _iso(timestamp: datetime) -> str:
    return require_utc(timestamp).isoformat()


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ResearchError("log timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ResearchError("log timestamp is not ISO-8601") from exc
    return require_utc(parsed)


def canonical(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def snapshot_hash(prices: Mapping[str, Sequence[tuple[datetime, Decimal]]]) -> str:
    lines: list[str] = []
    for code in sorted(prices):
        for timestamp, price in prices[code]:
            lines.append(f"{code},{_iso(timestamp)},{format(price, 'f')}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ForwardProposal:
    timestamp_utc: datetime
    model: str
    model_version: str
    prompt_hash: str
    inputs_snapshot_hash: str
    proposal: str
    rationale: str

    def __post_init__(self) -> None:
        require_utc(self.timestamp_utc)
        if not self.model or not self.model_version:
            raise ResearchError("proposal needs a model and a version")
        if len(self.prompt_hash) != 64 or len(self.inputs_snapshot_hash) != 64:
            raise ResearchError("proposal hashes must be sha256 hex")


def assert_inputs_before(proposal: ForwardProposal, price_timestamps: Sequence[datetime]) -> None:
    """Frozen-before-outcome: every input price is strictly earlier than the proposal."""
    _reject_prices(proposal.timestamp_utc, price_timestamps, strict=True)


def _reject_prices(moment: datetime, price_timestamps: Sequence[datetime], *, strict: bool) -> None:
    stamped = require_utc(moment)
    for timestamp in price_timestamps:
        observed = require_utc(timestamp)
        if strict and observed >= stamped:
            raise ResearchError("proposal inputs include a price that is not strictly before the proposal")
        if not strict and observed > stamped:
            raise ResearchError("snapshot includes a price after its timestamp")


def _json_safe(value: object, label: str) -> object:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool) or isinstance(value, float):
        raise ResearchError(f"{label} must not be a bool or a float")
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return [_json_safe(item, label) for item in value]
    if isinstance(value, dict):
        safe: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ResearchError(f"{label} keys must be strings")
            safe[key] = _json_safe(item, label)
        return safe
    raise ResearchError(f"{label} is not a loggable value")


def _seal(body: dict[str, object], previous: str) -> dict[str, object]:
    payload = dict(body)
    payload["prev_hash"] = previous
    payload["record_hash"] = _digest(payload)
    return payload


def _as_object(value: object, line_no: int) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ResearchError(f"forward log line {line_no} is not an object")
    return cast(dict[str, object], {str(key): item for key, item in value.items()})


class ProposalLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def create(self, *, now: datetime) -> None:
        if self.path.exists():
            raise ResearchError("forward log already exists; refusing to recreate it")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = _seal(
            {"schema": SCHEMA, "record_type": "log_created", "timestamp_utc": _iso(now)},
            GENESIS,
        )
        line = canonical(record) + "\n"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        descriptor = os.open(self.path, flags, 0o644)
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def records(self) -> tuple[dict[str, object], ...]:
        if not self.path.is_file():
            raise ResearchError("forward log does not exist")
        rows: list[dict[str, object]] = []
        for line_no, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchError(f"forward log line {line_no} is not JSON") from exc
            rows.append(_as_object(parsed, line_no))
        if not rows:
            raise ResearchError("forward log is empty")
        return tuple(rows)

    def created_at(self) -> datetime:
        first = self.records()[0]
        if first.get("record_type") != "log_created":
            raise ResearchError("forward log is missing its creation record")
        return _parse_utc(first.get("timestamp_utc"))

    def verify(self) -> int:
        previous = GENESIS
        rows = self.records()
        for index, row in enumerate(rows):
            stored = row.get("record_hash")
            body = {key: value for key, value in row.items() if key != "record_hash"}
            digest = _digest(body)
            if stored != digest or row.get("prev_hash") != previous:
                raise ResearchError(f"forward log chain broke at record {index}")
            if not isinstance(stored, str):
                raise ResearchError("forward log hash is missing")
            previous = stored
        if rows[0].get("record_type") != "log_created":
            raise ResearchError("forward log is missing its creation record")
        return len(rows)

    def _previous_hash(self, stamped: datetime) -> str:
        self.verify()
        created = self.created_at()
        if stamped < created:
            raise ResearchError("proposal is timestamped before the log was created")
        previous_row = self.records()[-1]
        previous_stamp = _parse_utc(previous_row.get("timestamp_utc"))
        if stamped <= previous_stamp:
            raise ResearchError("proposal timestamp is not after the previous log record")
        previous_hash = previous_row.get("record_hash")
        if not isinstance(previous_hash, str):
            raise ResearchError("previous log hash is missing")
        return previous_hash

    def _write(self, body: dict[str, object], previous_hash: str) -> None:
        record = _seal(body, previous_hash)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append_proposal(self, proposal: ForwardProposal, *, price_timestamps: Sequence[datetime]) -> None:
        assert_inputs_before(proposal, price_timestamps)
        stamped = require_utc(proposal.timestamp_utc)
        previous_hash = self._previous_hash(stamped)
        self._write(
            {
                "schema": SCHEMA,
                "record_type": "proposal",
                "timestamp_utc": _iso(stamped),
                "model": proposal.model,
                "model_version": proposal.model_version,
                "prompt_hash": proposal.prompt_hash,
                "inputs_snapshot_hash": proposal.inputs_snapshot_hash,
                "proposal": proposal.proposal,
                "rationale": proposal.rationale,
            },
            previous_hash,
        )

    def append_event(
        self,
        *,
        timestamp_utc: datetime,
        record_type: str,
        fields: Mapping[str, object],
        price_timestamps: Sequence[datetime] = (),
    ) -> None:
        """Append a snapshot, screen, or pre-registration record onto the same chain."""
        if record_type not in {"snapshot", "screen", "preregistration", "model_usage"}:
            raise ResearchError("forward log event type is not allowed")
        stamped = require_utc(timestamp_utc)
        _reject_prices(stamped, price_timestamps, strict=record_type != "snapshot")
        safe = _json_safe(dict(fields), record_type)
        if not isinstance(safe, dict):
            raise ResearchError("forward log fields must be an object")
        body: dict[str, object] = {"schema": SCHEMA, "record_type": record_type, "timestamp_utc": _iso(stamped)}
        for key, value in safe.items():
            if key in body or key in {"prev_hash", "record_hash"}:
                raise ResearchError(f"forward log field {key} is reserved")
            body[key] = value
        self._write(body, self._previous_hash(stamped))


def _weights(proposal: ForwardProposal) -> dict[str, Decimal]:
    try:
        parsed = json.loads(proposal.proposal)
    except json.JSONDecodeError as exc:
        raise ResearchError("proposal text is not JSON") from exc
    if not isinstance(parsed, dict):
        raise ResearchError("proposal JSON must be an object")
    raw = parsed.get("weights", {})
    if not isinstance(raw, dict):
        raise ResearchError("proposal weights must be an object")
    weights: dict[str, Decimal] = {}
    total = Decimal(0)
    for code, value in raw.items():
        if not isinstance(code, str) or not isinstance(value, str):
            raise ResearchError("proposal weights must be decimal strings")
        try:
            weight = Decimal(value)
        except InvalidOperation as exc:
            raise ResearchError(f"proposal weight for {code} is not a decimal") from exc
        if weight < 0:
            raise ResearchError("forward scorer refuses a short weight")
        weights[code] = weight
        total += weight
    if total > 1:
        raise ResearchError("forward scorer refuses gross exposure above 1")
    return weights


def _later(series: Sequence[tuple[datetime, Decimal]], proposal: ForwardProposal) -> tuple[tuple[datetime, Decimal], ...]:
    stamped = require_utc(proposal.timestamp_utc)
    return tuple((require_utc(timestamp), price) for timestamp, price in series if require_utc(timestamp) > stamped)


def score_proposal(
    proposal: ForwardProposal,
    gbp_value: Mapping[str, Sequence[tuple[datetime, Decimal]]],
) -> Decimal:
    """Spot return of the proposed weights using only later GBP values.

    Cash earns zero in this check. It is not the deposit simulator. A price
    at the proposal timestamp is an input, not an outcome, and is ignored.
    """
    weights = _weights(proposal)
    any_later = any(_later(series, proposal) for series in gbp_value.values())
    if not any_later:
        raise ResearchError("no price is strictly after the proposal timestamp")
    if not weights:
        return Decimal(0)
    total = Decimal(0)
    for code, weight in weights.items():
        series = _later(gbp_value.get(code, ()), proposal)
        if len(series) < 2:
            raise ResearchError(f"{code} has no entry and a later outcome after the proposal")
        entry = series[0][1]
        exit_price = series[-1][1]
        if entry <= 0 or exit_price <= 0:
            raise ResearchError(f"{code} outcome price is not positive")
        total += weight * (exit_price / entry - 1)
    return total


class GrokApiHook:
    """Disabled hook. There is no client, no URL, and no API key.

    Passing ``owner_approved=True`` still does not send a request. Wiring a
    client would be a separate change and is not present in this build.
    """

    def __init__(self, *, owner_approved: bool = False) -> None:
        self.owner_approved = owner_approved

    def complete(self, prompt: str) -> str:
        del prompt
        if self.owner_approved:
            raise ResearchIsolationError("Grok API hook is not implemented. Owner approval does not send a request in this build.")
        raise ResearchIsolationError(
            "Grok API hook is disabled. Enabling it requires explicit owner approval. No request was sent."
        )


class OfflineStubSource:
    """Deterministic offline proposal. It does not look at prices after the timestamp."""

    model = "offline-stub"
    model_version = "fx-forward-0"

    def propose(
        self,
        *,
        timestamp_utc: datetime,
        prompt: str,
        inputs: Mapping[str, Sequence[tuple[datetime, Decimal]]],
        rationale: str,
    ) -> ForwardProposal:
        stamped = require_utc(timestamp_utc)
        for series in inputs.values():
            for observed, _price in series:
                if require_utc(observed) >= stamped:
                    raise ResearchError("stub inputs include a price that is not before the proposal timestamp")
        body = json.dumps({"book": "gbp_cash", "weights": {}}, sort_keys=True, separators=(",", ":"))
        return ForwardProposal(
            timestamp_utc=stamped,
            model=self.model,
            model_version=self.model_version,
            prompt_hash=prompt_hash(prompt),
            inputs_snapshot_hash=snapshot_hash(inputs),
            proposal=body,
            rationale=rationale,
        )

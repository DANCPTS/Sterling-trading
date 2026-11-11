"""Check modes, clock order, and missed weeks on a forward log.

A missed week is recorded as MISSED. It is not filled in with a backdated
decision or a trade.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog, require_utc
from sterling.research.grok.portfolio import replay, state_hash
from sterling.research.grok.timekeeping import MODES

_SKEW = timedelta(seconds=5)


def verify_modes(records: Sequence[Mapping[str, object]], *, now: datetime) -> str:
    """Return the single mode. Reject mixtures, early responses, and future decisions."""
    stamped_now = require_utc(now)
    modes = [value for value in (record.get("mode") for record in records) if isinstance(value, str)]
    if not modes:
        raise ResearchError("forward log has no decision mode")
    mode = modes[0]
    if mode not in MODES or any(item != mode for item in modes):
        raise ResearchError("forward log mixes decision modes")
    for record in records:
        if "decision_at" not in record:
            continue
        decision_at = _time(record.get("decision_at"), "decision_at")
        request_at = _time(record.get("request_at"), "request_at")
        response_at = _time(record.get("response_at"), "response_at")
        recorded_at = _time(record.get("recorded_at"), "recorded_at")
        if response_at < request_at or request_at < decision_at:
            raise ResearchError("response time is before the request time")
        if recorded_at > stamped_now + _SKEW:
            raise ResearchError("recorded time is in the future")
        clock_source = record.get("clock_source")
        if mode == "forward":
            if clock_source != "system":
                raise ResearchError("forward mode requires the system clock")
            if decision_at > recorded_at + _SKEW:
                raise ResearchError("decision time is in the future")
            if recorded_at - decision_at > timedelta(minutes=30):
                raise ResearchError("forward decision is not the time it was written")
            if recorded_at + _SKEW < response_at:
                raise ResearchError("recorded time is before the response")
        elif mode in {"historical", "demo"}:
            if decision_at.date() == recorded_at.date():
                raise ResearchError("historical decision is recorded as if it were made today")
            if decision_at > recorded_at:
                raise ResearchError("historical decision time is after it was written")
        elif mode == "simulated_clock":
            if clock_source != "injected":
                raise ResearchError("simulated clock advance must be labelled injected")
    _week_claims(records)
    for index, record in enumerate(records):
        if record.get("record_type") != "state_commit":
            continue
        rebuilt = replay(records[: index + 1])
        if record.get("state_hash") != state_hash(rebuilt):
            raise ResearchError("state hash does not match the log replay")
    return mode


def verify_evidence_lane(records: Sequence[Mapping[str, object]], *, lane: str) -> None:
    """Reject a mock record in the forward log, and a forward record in the mock log."""
    if lane not in {"mock", "forward"}:
        raise ResearchError("unknown evidence lane")
    for record in records:
        value = record.get("evidence_lane")
        if lane == "forward" and value == "mock":
            raise ResearchError("mock record in the forward log")
        if lane == "mock" and value != "mock":
            raise ResearchError("forward record in the mock log")
        if isinstance(value, str) and value != lane:
            raise ResearchError("evidence log mixes mock and forward records")


def chronological_weeks(records: Sequence[Mapping[str, object]]) -> tuple[tuple[str, str], ...]:
    """Week order is the week key, not the order the lines were appended.

    A missed week is often written when a later week is checked. ``detected_at``
    is that moment. The returned list is the calendar order.
    """
    status: dict[str, str] = {}
    for record in records:
        week = record.get("week_key")
        kind = record.get("record_type")
        if not isinstance(week, str):
            continue
        if kind == "missed_week":
            status.setdefault(week, "missed")
        elif kind == "decision":
            status[week] = "decision"
    return tuple(sorted(status.items()))


def _week_claims(records: Sequence[Mapping[str, object]]) -> None:
    seen: dict[str, str] = {}
    for record in records:
        week = record.get("week_key")
        kind = record.get("record_type")
        if not isinstance(week, str) or kind not in {"decision", "missed_week"}:
            continue
        label = "missed" if kind == "missed_week" else "decision"
        previous = seen.get(week)
        if previous is not None and previous != label:
            raise ResearchError(f"{week} is both a decision and a missed week")
        seen[week] = label
        if kind == "missed_week" and "detected_at" not in record:
            raise ResearchError("missed week has no detection time")


def missing_weeks(records: Sequence[Mapping[str, object]], expected: Sequence[str]) -> tuple[str, ...]:
    present = {
        value
        for record in records
        if record.get("record_type") in {"decision", "missed_week", "week_reserve"}
        for value in [record.get("week_key")]
        if isinstance(value, str)
    }
    return tuple(week for week in expected if week not in present)


def append_missed(log: ProposalLog, weeks: Sequence[str], *, recorded_at: datetime, mode: str) -> int:
    """Append one MISSED record per absent week. No prices and no trades."""
    moment = require_utc(recorded_at)
    written = 0
    for week in weeks:
        moment = moment + timedelta(microseconds=1)
        log.append_event(
            timestamp_utc=moment,
            record_type="missed_week",
            fields={
                "clock_source": "system" if mode == "forward" else "injected",
                "detected_at": require_utc(recorded_at).isoformat(),
                "mode": mode,
                "note": "MISSED week. Not a backfill and not a trade.",
                "order": "detection",
                "recorded_at": require_utc(recorded_at).isoformat(),
                "week_key": week,
            },
        )
        written += 1
    return written


def _time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ResearchError(f"{label} is missing")
    from datetime import datetime as _datetime

    try:
        parsed = _datetime.fromisoformat(value)
    except ValueError as exc:
        raise ResearchError(f"{label} is not a timestamp") from exc
    return require_utc(parsed)

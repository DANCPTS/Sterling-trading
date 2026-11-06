"""JSONL persistence.

The file is the event log plus a checksum of the replayed state. Restart
replays the log; it does not take a fresh baseline from the wall clock.
Decimal amounts are strings so JSON numbers cannot become floats.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sterling.errors import MoneyError, PersistenceError
from sterling.ledger.calendar import london_date
from sterling.ledger.engine import equity_of, reduce_events
from sterling.ledger.events import EquitySnapshotEvent, LedgerEvent, event_from_dict, event_to_dict
from sterling.ledger.money import money_to_str, require_non_negative_money, signed_money

_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class LoadedLedger:
    initial_cash_gbp: Decimal
    events: tuple[LedgerEvent, ...]
    rules_version_id: str | None
    rules_content_hash: str | None


def write_ledger(
    path: Path,
    *,
    initial_cash_gbp: Decimal,
    events: list[LedgerEvent],
    rules_version_id: str | None,
    rules_content_hash: str | None,
) -> None:
    state = reduce_events(initial_cash_gbp, events)
    lines = [
        _dumps(
            {
                "record": "meta",
                "format_version": _FORMAT_VERSION,
                "currency": "GBP",
                "initial_cash_gbp": money_to_str(initial_cash_gbp),
                "rules_version_id": rules_version_id,
                "rules_content_hash": rules_content_hash,
            }
        )
    ]
    for event in events:
        payload: dict[str, object] = {"record": "event"}
        payload.update(event_to_dict(event))
        if isinstance(event, EquitySnapshotEvent):
            day = london_date(event.timestamp)
            payload["recorded_equity_gbp"] = money_to_str(state.baselines[day].equity_gbp)
        lines.append(_dumps(payload))
    baselines = [
        {
            "london_date": day.isoformat(),
            "equity_gbp": money_to_str(baseline.equity_gbp),
        }
        for day, baseline in sorted(state.baselines.items())
    ]
    lines.append(
        _dumps(
            {
                "record": "checksum",
                "cash_gbp": money_to_str(state.cash),
                "equity_gbp": money_to_str(equity_of(state)),
                "realised_pnl_gbp": money_to_str(state.realised),
                "event_count": len(events),
                "baselines": baselines,
            }
        )
    )
    _atomic_write(path, "\n".join(lines) + "\n")


def read_ledger(path: Path) -> LoadedLedger:
    text = Path(path).read_text(encoding="utf-8")
    raw_lines = [line for line in text.splitlines() if line.strip()]
    if len(raw_lines) < 2:
        raise PersistenceError("ledger file is missing meta or checksum")
    meta = _object(raw_lines[0])
    checksum = _object(raw_lines[-1])
    if meta.get("record") != "meta":
        raise PersistenceError("first record must be meta")
    if checksum.get("record") != "checksum":
        raise PersistenceError("last record must be checksum")
    if meta.get("format_version") != _FORMAT_VERSION:
        raise PersistenceError("unsupported ledger format_version")
    if meta.get("currency") != "GBP":
        raise PersistenceError("ledger currency must be GBP")
    try:
        initial = require_non_negative_money(meta.get("initial_cash_gbp"), label="initial_cash_gbp")
    except MoneyError as exc:
        raise PersistenceError("initial cash is unreadable") from exc
    rules_version_id = _optional_str(meta.get("rules_version_id"), "rules_version_id")
    rules_content_hash = _optional_str(meta.get("rules_content_hash"), "rules_content_hash")

    events: list[LedgerEvent] = []
    recorded: dict[str, Decimal] = {}
    for line in raw_lines[1:-1]:
        payload = _object(line)
        if payload.get("record") != "event":
            raise PersistenceError("middle records must be events")
        event = event_from_dict(payload)
        events.append(event)
        if isinstance(event, EquitySnapshotEvent):
            recorded_text = payload.get("recorded_equity_gbp")
            if not isinstance(recorded_text, str):
                raise PersistenceError("snapshot is missing recorded_equity_gbp")
            recorded[event.event_id] = require_non_negative_money(
                recorded_text,
                label="recorded_equity_gbp",
            )

    state = reduce_events(initial, events)
    if checksum.get("event_count") != len(events):
        raise PersistenceError("checksum event_count does not match the log")
    _expect_money(checksum.get("cash_gbp"), state.cash, "cash_gbp")
    _expect_money(checksum.get("equity_gbp"), equity_of(state), "equity_gbp")
    _expect_money(checksum.get("realised_pnl_gbp"), state.realised, "realised_pnl_gbp")
    expected_baselines = [
        {"london_date": day.isoformat(), "equity_gbp": money_to_str(baseline.equity_gbp)}
        for day, baseline in sorted(state.baselines.items())
    ]
    if checksum.get("baselines") != expected_baselines:
        raise PersistenceError("checksum baselines do not match replay")
    for event in events:
        if isinstance(event, EquitySnapshotEvent):
            day = london_date(event.timestamp)
            if recorded[event.event_id] != state.baselines[day].equity_gbp:
                raise PersistenceError("recorded snapshot equity does not match replay")
    return LoadedLedger(
        initial_cash_gbp=initial,
        events=tuple(events),
        rules_version_id=rules_version_id,
        rules_content_hash=rules_content_hash,
    )


def _dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _object(line: str) -> dict[str, Any]:
    try:
        raw = json.loads(line, parse_float=_reject_float)
    except json.JSONDecodeError as exc:
        raise PersistenceError("ledger line is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise PersistenceError("ledger line must be a JSON object")
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise PersistenceError("ledger keys must be strings")
        result[key] = value
    return result


def _reject_float(value: str) -> float:
    raise PersistenceError("JSON numbers with a fraction are not allowed: " + value)


def _optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PersistenceError(f"{label} must be a string or null")
    return value


def _expect_money(raw: object, actual: Decimal, label: str) -> None:
    try:
        parsed = signed_money(raw, label=label)
    except MoneyError as exc:
        raise PersistenceError(f"checksum {label} is unreadable") from exc
    if parsed != actual:
        raise PersistenceError(f"checksum {label} does not match replay")


def _atomic_write(path: Path, text: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise

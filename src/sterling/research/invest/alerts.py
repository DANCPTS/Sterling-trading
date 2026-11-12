"""Offline text for a later read-only Telegram sender. This module does not send."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def event_hash(record: Mapping[str, object]) -> str:
    """Prefer the evidence hash. Otherwise hash the canonical record."""
    stored = record.get("record_hash")
    if isinstance(stored, str) and len(stored) == 64:
        return stored
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def format_alert(record: Mapping[str, object]) -> str | None:
    """One read-only line, or None when the record is not an alert."""
    kind = record.get("record_type")
    lane = record.get("evidence_lane") or "unlabelled"
    if kind == "decision":
        return f"{lane} decision {record.get('gate')} {record.get('timestamp_utc')}"
    if kind == "fill":
        return f"{lane} fill {record.get('side')} {record.get('fund')} {record.get('session')}"
    if kind == "fill_skipped" and record.get("reason") == "EXPIRED":
        return f"{lane} EXPIRED {record.get('fund')} {record.get('session')}"
    if kind == "valuation":
        return f"{lane} valuation {record.get('session')} nav {record.get('nav_gbp')}"
    return None


class AlertDedup:
    """Drop a repeated event hash. Nothing is sent from here."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def accept(self, record: Mapping[str, object]) -> str | None:
        digest = event_hash(record)
        text = format_alert(record)
        if text is None or digest in self.seen:
            return None
        self.seen.add(digest)
        return text

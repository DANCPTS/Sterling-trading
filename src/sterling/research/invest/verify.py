"""Reject a paper-v2 log that mixes versions or lanes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sterling.research.errors import ResearchError
from sterling.research.grok.verify import verify_evidence_lane
from sterling.research.invest.identity import EXPERIMENT_V2, SYSTEM_INVEST


def verify_invest(records: Sequence[Mapping[str, object]], *, lane: str) -> None:
    verify_evidence_lane(records, lane=lane)
    for record in records:
        if record.get("system") != SYSTEM_INVEST or record.get("experiment") != EXPERIMENT_V2:
            raise ResearchError("paper-v2 log contains a record from another experiment")
        permission = record.get("execution_permission")
        if permission is not None and permission != "NOT_ISSUED":
            raise ResearchError("paper-v2 record issued execution permission")
    _unique(records, "valuation")
    _unique(records, "cash_interest")
    _unique_fills(records)


def _unique(records: Sequence[Mapping[str, object]], kind: str) -> None:
    seen: set[str] = set()
    for record in records:
        if record.get("record_type") != kind:
            continue
        session = record.get("session")
        if not isinstance(session, str) or session in seen:
            raise ResearchError(f"duplicate {kind} for one session")
        seen.add(session)


def _unique_fills(records: Sequence[Mapping[str, object]]) -> None:
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        kind = record.get("record_type")
        if kind not in {"fill", "fill_skipped"}:
            continue
        key = (str(record.get("session")), str(record.get("fund")), str(kind))
        if key in seen:
            raise ResearchError("duplicate fill for one fund on one session")
        seen.add(key)


def verify_v1_closed(records: Sequence[Mapping[str, object]]) -> None:
    """The original forward log stays v1. A paper-v2 line does not belong in it."""
    for record in records:
        if record.get("experiment") not in (None, "grok-forward-v1"):
            raise ResearchError("v1 log contains another experiment")
        if record.get("system") not in (None,):
            raise ResearchError("v1 log contains another system")

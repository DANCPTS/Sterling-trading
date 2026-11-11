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


def verify_v1_closed(records: Sequence[Mapping[str, object]]) -> None:
    """The original forward log stays v1. A paper-v2 line does not belong in it."""
    for record in records:
        if record.get("experiment") not in (None, "grok-forward-v1"):
            raise ResearchError("v1 log contains another experiment")
        if record.get("system") not in (None,):
            raise ResearchError("v1 log contains another system")

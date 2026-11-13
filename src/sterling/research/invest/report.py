"""Performance page rebuilt only from a verified evidence log.

The page is one system section per ``system`` id. Sterling FX is not built;
a later log with ``system=sterling-fx`` would render as its own section.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.invest.verify import verify_invest


def write_dashboard(log_path: Path, destination: Path, *, evidence_note: str = "") -> int:
    """Write HTML only when the hash chain and the paper-v2 rules both pass.

    A failed check or a failed write does not replace ``destination``.
    The new page is written beside it and moved into place only after the
    write finishes.
    """
    try:
        log = ProposalLog(log_path)
        log.verify()
        lanes = {record.get("evidence_lane") for record in log.records()}
        lane = "mock" if lanes == {"mock"} else "forward"
        verify_invest(log.records(), lane=lane)
        from sterling.research.invest.dashboard import render_dashboard

        page = render_dashboard(log.records(), evidence_note=evidence_note)
        _publish_text(destination, page)
    except (OSError, ResearchError):
        return 1
    return 0


def _publish_text(destination: Path, text: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _money(value: object) -> str:
    amount = _decimal(value)
    if amount is None:
        return ""
    return f"{amount:,.2f}"


def _pct(value: object) -> str:
    amount = _decimal(value)
    if amount is None:
        return ""
    return f"{amount * 100:.2f}%"


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

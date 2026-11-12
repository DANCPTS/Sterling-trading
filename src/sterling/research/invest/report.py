"""Performance page rebuilt only from a verified evidence log.

The page is one system section per ``system`` id. Sterling FX is not built;
a later log with ``system=sterling-fx`` would render as its own section.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.invest.verify import verify_invest


def write_dashboard(log_path: Path, destination: Path) -> int:
    """Write HTML only when the hash chain and the paper-v2 rules both pass.

    A failed check does not replace ``destination``.
    """
    try:
        log = ProposalLog(log_path)
        log.verify()
        lanes = {record.get("evidence_lane") for record in log.records()}
        lane = "mock" if lanes == {"mock"} else "forward"
        verify_invest(log.records(), lane=lane)
        from sterling.research.invest.dashboard import render_dashboard

        page = render_dashboard(log.records())
    except (OSError, ResearchError):
        return 1
    destination.write_text(page, encoding="utf-8")
    return 0


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

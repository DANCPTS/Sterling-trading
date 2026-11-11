"""Performance page rebuilt only from a verified evidence log.

The page is one system section per ``system`` id. Sterling FX is not built;
a later log with ``system=sterling-fx`` would render as its own section.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.invest.verify import verify_invest


def write_dashboard(log_path: Path, destination: Path) -> int:
    """Write HTML only when the hash chain and the paper-v2 rules both pass."""
    try:
        log = ProposalLog(log_path)
        log.verify()
        lanes = {record.get("evidence_lane") for record in log.records()}
        lane = "mock" if lanes == {"mock"} else "forward"
        verify_invest(log.records(), lane=lane)
    except (OSError, ResearchError):
        return 1
    destination.write_text(_html(log.records()), encoding="utf-8")
    return 0


def _html(records: tuple[Mapping[str, object], ...] | list[Mapping[str, object]]) -> str:
    systems = sorted({str(record.get("system")) for record in records if record.get("system")})
    valuations = [record for record in records if record.get("record_type") == "valuation"]
    performances = [record for record in records if record.get("record_type") == "performance"]
    latest = valuations[-1] if valuations else {}
    performance = performances[-1] if performances else {}
    raw_arms = performance.get("arms")
    arm_rows = [item for item in raw_arms if isinstance(item, dict)] if isinstance(raw_arms, list) else []
    label = str(latest.get("data_label") or performance.get("data_label") or "unlabelled")
    rows = "\n".join(_row(record) for record in valuations)
    bench = "\n".join(_arm(arm) for arm in arm_rows)
    sections = "\n".join(f'<section data-system="{name}"><h2>{name}</h2></section>' for name in systems)
    nav = latest.get("nav_gbp", "")
    cash = latest.get("cash_gbp", "")
    drawdown = latest.get("drawdown", "")
    costs = latest.get("cumulative_costs_gbp", "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Sterling performance</title>
<style>
body {{ font-family: Georgia, serif; margin: 2rem; color: #1c1915; background: #f6f3ec; }}
h1 {{ font-weight: normal; }}
.banner {{ background: #3f3a33; color: #f6f3ec; padding: 0.6rem 1rem; letter-spacing: 0.04em; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0 2rem; }}
th, td {{ border-bottom: 1px solid #d9d2c5; text-align: right; padding: 0.35rem 0.5rem; }}
th:first-child, td:first-child {{ text-align: left; }}
section {{ margin-top: 1.5rem; }}
</style>
</head>
<body>
<p class="banner">DATA LABEL: {label}. This page is not a live broker account.</p>
<h1>Sterling performance</h1>
{sections}
<p>Latest portfolio value {nav} GBP. Cash {cash} GBP. Drawdown {drawdown}. Dealing costs {costs} GBP.</p>
<h2>Daily marks</h2>
<table>
<thead><tr><th>Date</th><th>Value</th><th>Cash</th><th>Daily P&amp;L</th><th>Return</th><th>Drawdown</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
<h2>Benchmarks on the same dates</h2>
<table>
<thead><tr><th>Book</th><th>End value</th><th>Return</th><th>Max drawdown</th><th>Trades</th><th>Costs</th></tr></thead>
<tbody>
{bench}
</tbody>
</table>
<p>A future Sterling FX book would appear as another section with its own system id and its own balance. None is included here.</p>
</body>
</html>
"""


def _row(record: Mapping[str, object]) -> str:
    return (
        "<tr>"
        f"<td>{record.get('session')}</td>"
        f"<td>{record.get('nav_gbp')}</td>"
        f"<td>{record.get('cash_gbp')}</td>"
        f"<td>{record.get('daily_pnl_gbp')}</td>"
        f"<td>{record.get('cumulative_return')}</td>"
        f"<td>{record.get('drawdown')}</td>"
        "</tr>"
    )


def _arm(arm: Mapping[str, object]) -> str:
    return (
        "<tr>"
        f"<td>{arm.get('name')}</td>"
        f"<td>{arm.get('end_nav_gbp')}</td>"
        f"<td>{arm.get('cumulative_return')}</td>"
        f"<td>{arm.get('max_drawdown')}</td>"
        f"<td>{arm.get('trades')}</td>"
        f"<td>{arm.get('costs_gbp')}</td>"
        "</tr>"
    )

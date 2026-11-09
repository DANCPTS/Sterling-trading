"""CSV, JSON, and console reports for a research run."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from sterling.research.engine import SimulationResult
from sterling.research.metrics import Performance

MONEY = Decimal("0.01")


def _money(value: Decimal) -> str:
    return format(value.quantize(MONEY), "f")


def _ratio(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.quantize(Decimal("0.0001")), "f")


def _performance_dict(metrics: Performance) -> dict[str, object]:
    payload = asdict(metrics)
    rendered: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, Decimal):
            rendered[key] = format(value, "f")
        elif hasattr(value, "isoformat"):
            rendered[key] = value.isoformat()
        else:
            rendered[key] = value
    return rendered


def format_report(result: SimulationResult, *, limit_lines: tuple[str, ...] = ()) -> str:
    lines = [
        f"Sterling research simulator — {result.label}",
        "Synthetic or local research run. Not an official performance result. Not permission to trade.",
        f"mode={result.mode} execution_permission={result.execution_permission} orders_submitted={result.orders_submitted}",
        f"OCF deducted GBP {_money(result.ocf_deducted_gbp)}",
        "",
        "Fills (next-session open, costs included):",
        "date signal side ticker qty mid exec notional commission spread slippage stamp fx cash_after",
    ]
    if not result.fills:
        lines.append("(no fills)")
    for fill in result.fills:
        signal = fill.signal_date.isoformat() if fill.signal_date else "-"
        lines.append(
            " ".join(
                [
                    fill.session.isoformat(),
                    signal,
                    fill.side,
                    fill.ticker,
                    format(fill.quantity, "f"),
                    _money(fill.mid_price_gbp),
                    format(fill.exec_price_gbp, "f"),
                    _money(fill.notional_gbp),
                    _money(fill.commission_gbp),
                    _money(fill.spread_cost_gbp),
                    _money(fill.slippage_cost_gbp),
                    _money(fill.stamp_duty_gbp),
                    _money(fill.fx_cost_gbp),
                    _money(fill.cash_after_gbp),
                ]
            )
        )
    lines.extend(["", "NAV on month-ends and fill dates:", "date cash market_value nav gross cash_weight"])
    fill_days = {fill.session for fill in result.fills}
    for row in result.nav:
        later_same_month = any(
            other.session.year == row.session.year and other.session.month == row.session.month and other.session > row.session
            for other in result.nav
        )
        if later_same_month and row.session not in fill_days and row is not result.nav[0]:
            continue
        lines.append(
            " ".join(
                [
                    row.session.isoformat(),
                    _money(row.cash_gbp),
                    _money(row.market_value_gbp),
                    _money(row.nav_gbp),
                    _ratio(row.gross_exposure),
                    _ratio(row.cash_weight),
                ]
            )
        )
    metrics = result.metrics
    lines.extend(
        [
            "",
            "Strategy metrics (full window passed to this run):",
            f"start_nav {_money(metrics.start_nav_gbp)} end_nav {_money(metrics.end_nav_gbp)}",
            f"cagr {_ratio(metrics.cagr)} vol {_ratio(metrics.annualised_volatility)} "
            f"max_dd {_ratio(metrics.max_drawdown)} dd_days {metrics.max_drawdown_duration_days} "
            f"underwater_days {metrics.max_drawdown_underwater_days}",
            f"sharpe {_ratio(metrics.sharpe)} sortino {_ratio(metrics.sortino)} calmar {_ratio(metrics.calmar)}",
            f"monthly_win_rate {_ratio(metrics.monthly_win_rate)} avg_win {_ratio(metrics.average_win)} "
            f"avg_loss {_ratio(metrics.average_loss)}",
            f"turnover {_ratio(metrics.total_turnover)} cost_gbp {_money(metrics.total_cost_gbp)} "
            f"cost_drag {_ratio(metrics.total_cost_drag)} annualised_cost_drag {_ratio(metrics.annualised_cost_drag)}",
            f"avg_cash {_ratio(metrics.average_cash_weight)} days_in_cash {_ratio(metrics.fraction_days_in_cash)} "
            f"avg_exposure {_ratio(metrics.average_gross_exposure)}",
            "",
            "Benchmarks (buy at the first open, then hold; not passed through the single-name cap):",
            "name end_nav cagr vol max_dd sharpe cost_gbp",
        ]
    )
    lines.append(
        "strategy "
        + " ".join(
            [
                _money(metrics.end_nav_gbp),
                _ratio(metrics.cagr),
                _ratio(metrics.annualised_volatility),
                _ratio(metrics.max_drawdown),
                _ratio(metrics.sharpe),
                _money(metrics.total_cost_gbp),
            ]
        )
    )
    for name, bench in result.benchmarks:
        lines.append(
            " ".join(
                [
                    name,
                    _money(bench.end_nav_gbp),
                    _ratio(bench.cagr),
                    _ratio(bench.annualised_volatility),
                    _ratio(bench.max_drawdown),
                    _ratio(bench.sharpe),
                    _money(bench.total_cost_gbp),
                ]
            )
        )
    if result.decisions:
        lines.extend(["", "Proposals and gate decisions:"])
        for decision in result.decisions:
            reasons = decision.get("reasons")
            reason_text = "; ".join(str(item) for item in reasons) if isinstance(reasons, list) else ""
            lines.append(
                f"{decision.get('signal_date')} cutoff {decision.get('data_cutoff')} "
                f"weights {decision.get('weights')} cash {decision.get('cash_weight')} {reason_text}"
            )
    if result.caveats:
        lines.extend(["", "Notes:"])
        lines.extend(f"- {note}" for note in result.caveats)
    if limit_lines:
        lines.extend(["", "Proposed research risk limits (NOT APPROVED):"])
        lines.extend(limit_lines)
    return "\n".join(lines) + "\n"


def write_report(result: SimulationResult, directory: Path, *, limit_lines: tuple[str, ...] = ()) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "nav.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "cash_gbp", "market_value_gbp", "nav_gbp", "gross_exposure", "cash_weight"])
        for row in result.nav:
            writer.writerow(
                [
                    row.session.isoformat(),
                    format(row.cash_gbp, "f"),
                    format(row.market_value_gbp, "f"),
                    format(row.nav_gbp, "f"),
                    format(row.gross_exposure, "f"),
                    format(row.cash_weight, "f"),
                ]
            )
    with (directory / "fills.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "date",
                "signal_date",
                "side",
                "ticker",
                "quantity",
                "mid_gbp",
                "exec_gbp",
                "notional_gbp",
                "commission_gbp",
                "spread_gbp",
                "slippage_gbp",
                "stamp_gbp",
                "fx_gbp",
                "cash_after_gbp",
            ]
        )
        for fill in result.fills:
            writer.writerow(
                [
                    fill.session.isoformat(),
                    fill.signal_date.isoformat() if fill.signal_date else "",
                    fill.side,
                    fill.ticker,
                    format(fill.quantity, "f"),
                    format(fill.mid_price_gbp, "f"),
                    format(fill.exec_price_gbp, "f"),
                    format(fill.notional_gbp, "f"),
                    format(fill.commission_gbp, "f"),
                    format(fill.spread_cost_gbp, "f"),
                    format(fill.slippage_cost_gbp, "f"),
                    format(fill.stamp_duty_gbp, "f"),
                    format(fill.fx_cost_gbp, "f"),
                    format(fill.cash_after_gbp, "f"),
                ]
            )
    (directory / "metrics.json").write_text(
        json.dumps(_performance_dict(result.metrics), indent=2) + "\n",
        encoding="utf-8",
    )
    (directory / "benchmarks.json").write_text(
        json.dumps({name: _performance_dict(item) for name, item in result.benchmarks}, indent=2) + "\n",
        encoding="utf-8",
    )
    with (directory / "proposals.jsonl").open("w", encoding="utf-8") as handle:
        for record in result.proposals:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        for record in result.decisions:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (directory / "summary.txt").write_text(format_report(result, limit_lines=limit_lines), encoding="utf-8")

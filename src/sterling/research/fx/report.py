"""Text tables for the FX study. Numbers are formatted here; nothing is refit."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from sterling.research.fx.simulate import FxResult
from sterling.research.fx.stats import BootstrapCI, WalkWindow
from sterling.research.metrics import Performance

PENCE = Decimal("0.01")


def pct(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return format((value * Decimal(100)).quantize(PENCE, rounding=ROUND_HALF_EVEN), "f")


def num(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return format(value.quantize(PENCE, rounding=ROUND_HALF_EVEN), "f")


def money(value: Decimal) -> str:
    return format(value.quantize(PENCE, rounding=ROUND_HALF_EVEN), "f")


def _ci(interval: BootstrapCI | None, *, as_percent: bool) -> str:
    if interval is None:
        return "n/a"
    render = pct if as_percent else num
    return f"{render(interval.low)} to {render(interval.high)} (point {render(interval.point)}, draws {interval.draws})"


@dataclass(frozen=True, slots=True)
class PathReport:
    result: FxResult
    sharpe_vs_cash: Decimal | None
    excess_cagr: Decimal | None
    mean_excess: BootstrapCI | None
    sharpe_ci_vs_cash: BootstrapCI | None
    sharpe_ci_vs_basket: BootstrapCI | None
    worst_month: tuple[date, Decimal] | None
    worst_12m: tuple[date, Decimal] | None
    recovery: str
    years: tuple[tuple[int, Decimal, str], ...]
    crises: tuple[tuple[str, Decimal | None], ...]
    oos_start: date
    oos_cagr: Decimal | None
    oos_excess_cagr: Decimal | None
    oos_sharpe_vs_cash: Decimal | None
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GridRow:
    name: str
    cagr: Decimal | None
    volatility: Decimal | None
    max_drawdown: Decimal
    sharpe_vs_cash: Decimal | None
    excess_cagr: Decimal | None
    trades: int
    costs_gbp: Decimal


def _perf_line(result: FxResult, sharpe_vs_cash: Decimal | None, excess: Decimal | None) -> str:
    path: Performance = result.performance
    cells = [
        result.name,
        pct(path.cagr),
        pct(path.annualised_volatility),
        pct(path.max_drawdown),
        num(path.sharpe),
        num(sharpe_vs_cash),
        num(path.sortino),
        num(path.calmar),
        str(result.trades),
        num(result.one_way_turnover),
        money(result.total_cost_gbp),
        pct(excess),
        pct(path.average_cash_weight),
    ]
    return " | ".join(cells)


def render_report(
    *,
    banner: str,
    spans: Sequence[str],
    notes: Sequence[str],
    config_sha256: str,
    sample_start: date,
    sample_end: date,
    decisions: int,
    oos_start: date,
    paths: Sequence[PathReport],
    cash: PathReport,
    basket: PathReport,
    grid: Sequence[GridRow],
    walk_trend: Sequence[WalkWindow],
    walk_carry: Sequence[WalkWindow],
    combined_blockers: Sequence[str],
    combined: GridRow | None,
    stale_notes: Sequence[str],
) -> str:
    header = (
        "name | net CAGR % | vol % | max DD % | Sharpe rf0 | Sharpe vs cash | Sortino | Calmar | "
        "trades | one-way turnover | costs GBP | CAGR minus cash pp | avg cash weight %"
    )
    lines: list[str] = [
        banner,
        "execution_permission=NOT_ISSUED orders_submitted=0 mode=research leverage=none short_selling=prohibited",
        f"config_sha256={config_sha256}",
        f"sample {sample_start.isoformat()} to {sample_end.isoformat()} decisions={decisions} oos_from={oos_start.isoformat()}",
        "",
        "Data actually loaded",
        *spans,
        "",
        "Assumptions fixed before this run",
        *notes,
        "",
        "Headline and benchmarks",
        header,
    ]
    for path in (*paths, cash, basket):
        lines.append(_perf_line(path.result, path.sharpe_vs_cash, path.excess_cagr))
    lines.extend(["", "Drawdowns, tails, and excess return over the GBP deposit"])
    for path in (*paths, cash, basket):
        worst_m = "n/a" if path.worst_month is None else f"{path.worst_month[0].isoformat()} {pct(path.worst_month[1])}%"
        worst_y = "n/a" if path.worst_12m is None else f"{path.worst_12m[0].isoformat()} {pct(path.worst_12m[1])}%"
        lines.append(f"{path.result.name}: {path.recovery}")
        lines.append(f"  worst month {worst_m}; worst 12 month-end intervals {worst_y}")
        lines.append(
            "  annualised mean excess over GBP cash, 90% block-bootstrap CI, percent: " + _ci(path.mean_excess, as_percent=True)
        )
        lines.append("  Sharpe of monthly excess over GBP cash, 90% CI: " + _ci(path.sharpe_ci_vs_cash, as_percent=False))
        lines.append(
            "  Sharpe of monthly excess over the equal-weight basket, 90% CI: " + _ci(path.sharpe_ci_vs_basket, as_percent=False)
        )
        lines.append(
            f"  out of sample from {path.oos_start.isoformat()}: CAGR {pct(path.oos_cagr)}% "
            f"excess CAGR {pct(path.oos_excess_cagr)} pp Sharpe vs cash {num(path.oos_sharpe_vs_cash)}"
        )
    lines.extend(["", "Calendar-year returns, percent. Partial years are marked."])
    for path in (*paths, cash, basket):
        rendered = ", ".join(f"{year} {pct(value)} ({flag})" for year, value, flag in path.years)
        lines.append(f"{path.result.name}: {rendered}")
    lines.extend(["", "Crisis windows, percent return"])
    for path in (*paths, cash, basket):
        rendered = ", ".join(f"{name} {pct(value)}" for name, value in path.crises)
        lines.append(f"{path.result.name}: {rendered}")
    lines.extend(
        [
            "",
            "Sensitivity grid. Not used to choose the headline rules.",
            "name | net CAGR % | vol % | max DD % | Sharpe vs cash | CAGR minus cash pp | trades | costs GBP",
        ]
    )
    for row in grid:
        lines.append(
            " | ".join(
                [
                    row.name,
                    pct(row.cagr),
                    pct(row.volatility),
                    pct(row.max_drawdown),
                    num(row.sharpe_vs_cash),
                    pct(row.excess_cagr),
                    str(row.trades),
                    money(row.costs_gbp),
                ]
            )
        )
    lines.extend(["", "Walk-forward test windows, rules frozen. Excess is CAGR minus the GBP deposit CAGR, percentage points."])
    lines.append(_walk("trend", walk_trend))
    lines.append(_walk("carry", walk_carry))
    for label, windows in (("trend", walk_trend), ("carry", walk_carry)):
        for window in windows:
            lines.append(
                f"  {label} {window.split.test_start.isoformat()} {window.split.test_end.isoformat()} "
                f"cagr {pct(window.strategy_cagr)} excess {pct(window.excess_cagr)}"
            )
    lines.extend(["", "Combined trend-and-carry"])
    if combined_blockers:
        lines.append("Not a headline result. The pre-registered justification failed:")
        lines.extend(f"- {item}" for item in combined_blockers)
    else:
        lines.append(
            "Both standalone legs cleared the pre-registered full-sample and out-of-sample bar, so the intersection is reported."
        )
    if combined is not None:
        lines.append(
            "Pre-registered intersection, shown so the gate is visible: "
            f"CAGR {pct(combined.cagr)}% vol {pct(combined.volatility)}% max DD {pct(combined.max_drawdown)}% "
            f"Sharpe vs cash {num(combined.sharpe_vs_cash)} excess CAGR {pct(combined.excess_cagr)} pp "
            f"trades {combined.trades} costs GBP {money(combined.costs_gbp)}"
        )
    if stale_notes:
        lines.extend(["", "Stale policy-rate accrual (sessions a held balance earned zero because the lagged rate was missing)"])
        lines.extend(stale_notes)
    lines.extend(["", "Verdict"])
    for path in paths:
        if path.failures:
            detail = " ".join(item[0].upper() + item[1:] + "." for item in path.failures)
            lines.append(
                f"{path.result.name}: no credible trading advantage under the headline retail spreads, "
                "round-trip platform fee, and deposit haircuts. " + detail
            )
        else:
            lines.append(
                f"{path.result.name}: the pre-registered bar was met on this historical sample "
                "(positive net CAGR, full-sample and out-of-sample CAGR above the GBP deposit, "
                "positive Sharpe versus that deposit, and a 90% block-bootstrap interval for the "
                "annualised mean excess return above zero). That is not a forecast and it is not permission to trade."
            )
    lines.append(
        "The GBP 2.50 per-trade figure and the GBP 5.00 daily figure are operational risk caps, "
        "not position-size limits, and this study does not use them. No order was submitted."
    )
    lines.append("")
    return "\n".join(lines)


def _walk(label: str, windows: Sequence[WalkWindow]) -> str:
    excesses = [window.excess_cagr for window in windows if window.excess_cagr is not None]
    if not excesses:
        return f"{label}: no walk-forward test window"
    positive = sum(1 for value in excesses if value > 0)
    ordered = sorted(excesses)
    middle = ordered[len(ordered) // 2]
    return (
        f"{label}: windows {len(windows)} median excess CAGR {pct(middle)} pp "
        f"min {pct(ordered[0])} max {pct(ordered[-1])} positive {positive}/{len(excesses)}"
    )

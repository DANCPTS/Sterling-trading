"""Run the pre-registered FX study from a local cache. Nothing is downloaded."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

from sterling.research.errors import ResearchError
from sterling.research.fx.config import FxResearchConfig, load_fx_config
from sterling.research.fx.report import GridRow, PathReport, render_report
from sterling.research.fx.series import Market, load_market
from sterling.research.fx.simulate import FxResult, decision_schedule, run_path
from sterling.research.fx.stats import (
    BootstrapCI,
    advantage_failures,
    bootstrap_mean_excess,
    bootstrap_sharpe_diff,
    calendar_year_returns,
    excess_cagr,
    oos_start,
    paired_monthly,
    recovery_text,
    slice_rows,
    walk_forward_excess,
    window_return,
    worst_month,
    worst_year,
)
from sterling.research.fx.strategies import Carry, Combined, LegScore, Trend, combined_blockers
from sterling.research.metrics import excess_sharpe, performance

BANNER = (
    "RESEARCH SIMULATION - unleveraged long-only foreign-currency deposits funded from GBP. "
    "Not operational, not a live result, and not an execution path."
)


def _spans(market: Market) -> tuple[str, ...]:
    lines = [
        (
            f"panel {market.panel[0].isoformat()} to {market.panel[-1].isoformat()} "
            f"n={len(market.panel)} days with a print for every currency"
        ),
        (
            f"SONIA IUDSOIA observed {market.sonia.observed[0].isoformat()} to {market.sonia.observed[-1].isoformat()} "
            f"n={len(market.sonia.observed)} usable from the next London business day"
        ),
        (
            f"Bank Rate IUDBEDR observed {market.bank_rate.observed[0].isoformat()} to "
            f"{market.bank_rate.observed[-1].isoformat()} n={len(market.bank_rate.observed)} same lag"
        ),
    ]
    codes = {item.code: item for item in market.currencies}
    for code, spot_series in market.spots:
        spec = codes[code]
        lines.append(
            f"{code} {spec.boe_series} spot {spot_series[0][0].isoformat()} to {spot_series[-1][0].isoformat()} "
            f"n={len(spot_series)} BoE IADB, foreign currency per GBP"
        )
    for code, policy_series in market.policy:
        spec = codes[code]
        lines.append(
            f"{code} BIS {spec.bis_area} policy {policy_series.observed[0].isoformat()} to "
            f"{policy_series.observed[-1].isoformat()} n={len(policy_series.observed)} monthly, "
            "usable from the 15th of month+2"
        )
    return tuple(lines)


def _grid(result: FxResult, cash: FxResult) -> GridRow:
    return GridRow(
        name=result.name,
        cagr=result.performance.cagr,
        volatility=result.performance.annualised_volatility,
        max_drawdown=result.performance.max_drawdown,
        sharpe_vs_cash=excess_sharpe(result.rows, cash.rows),
        excess_cagr=excess_cagr(result.rows, cash.rows),
        trades=result.trades,
        costs_gbp=result.total_cost_gbp,
    )


def _mean_ci(result: FxResult, other: FxResult, config: FxResearchConfig, seed: int) -> BootstrapCI | None:
    left, right = paired_monthly(result.rows, other.rows)
    try:
        return bootstrap_mean_excess(
            left,
            right,
            block=config.bootstrap_block_months,
            replications=config.bootstrap_replications,
            seed=seed,
            ci=config.bootstrap_ci,
        )
    except ResearchError:
        return None


def _sharpe_ci(result: FxResult, other: FxResult, config: FxResearchConfig, seed: int) -> BootstrapCI | None:
    left, right = paired_monthly(result.rows, other.rows)
    try:
        return bootstrap_sharpe_diff(
            left,
            right,
            block=config.bootstrap_block_months,
            replications=config.bootstrap_replications,
            seed=seed,
            ci=config.bootstrap_ci,
        )
    except ResearchError:
        return None


def _describe(
    result: FxResult,
    cash: FxResult,
    basket: FxResult,
    config: FxResearchConfig,
    *,
    oos_on: date,
    seed: int,
) -> PathReport:
    end = result.rows[-1].session
    oos_rows = slice_rows(result.rows, oos_on, end)
    oos_cash = slice_rows(cash.rows, oos_on, end)
    sharpe_vs_cash = excess_sharpe(result.rows, cash.rows)
    excess = excess_cagr(result.rows, cash.rows)
    mean_excess = _mean_ci(result, cash, config, seed)
    oos_excess = excess_cagr(oos_rows, oos_cash) if len(oos_rows) >= 2 and len(oos_cash) >= 2 else None
    failures: tuple[str, ...] = ()
    if result.group == "headline":
        failures = advantage_failures(
            cagr=result.performance.cagr,
            excess=excess,
            sharpe_vs_cash=sharpe_vs_cash,
            oos_excess=oos_excess,
            excess_ci_low=None if mean_excess is None else mean_excess.low,
        )
    oos_cagr = None
    oos_sharpe = None
    if len(oos_rows) >= 2:
        oos_cagr = performance(oos_rows, total_cost_gbp=Decimal(0), total_turnover=Decimal(0)).cagr
    if len(oos_rows) >= 2 and len(oos_cash) >= 2:
        oos_sharpe = excess_sharpe(oos_rows, oos_cash)
    crises = tuple((window.name, window_return(result.rows, window.start, window.end)) for window in config.performance_windows)
    return PathReport(
        result=result,
        sharpe_vs_cash=sharpe_vs_cash,
        excess_cagr=excess,
        mean_excess=mean_excess,
        sharpe_ci_vs_cash=_sharpe_ci(result, cash, config, seed),
        sharpe_ci_vs_basket=None if result.name == basket.name else _sharpe_ci(result, basket, config, seed + 7),
        worst_month=worst_month(result.rows),
        worst_12m=worst_year(result.rows),
        recovery=recovery_text(result.rows),
        years=calendar_year_returns(result.rows),
        crises=crises,
        oos_start=oos_on,
        oos_cagr=oos_cagr,
        oos_excess_cagr=oos_excess,
        oos_sharpe_vs_cash=oos_sharpe,
        failures=failures,
    )


def run_study(cache: Path, output: Path) -> str:
    """Score the fixed rules. This does not write a Grok proposal log."""
    config = load_fx_config()
    market = load_market(cache, config)
    schedule = decision_schedule(market.panel, config.min_month_ends)
    trend_rule = Trend(months=config.trend_months, rule="sma", weighting="equal", strategy_id="fx_trend_sma_10")
    carry_rule = Carry(k=config.carry_k, threshold=config.carry_threshold, strategy_id="fx_carry_k3")

    def go(
        name: str,
        group: str,
        mode: str,
        strategy: Trend | Carry | Combined | None,
        *,
        cost_name: str | None = None,
        deposit_name: str | None = None,
    ) -> FxResult:
        return run_path(
            market=market,
            schedule=schedule,
            strategy=strategy,
            deposit=config.deposit(deposit_name or config.primary_deposit_scenario),
            cost=config.cost(cost_name or config.primary_cost_scenario),
            config=config,
            mode=mode,
            name=name,
            group=group,
        )

    trend = go("trend", "headline", "signal", trend_rule)
    carry = go("carry", "headline", "signal", carry_rule)
    cash = go("gbp_cash", "benchmark", "cash", None)
    basket = go("equal_weight_basket", "benchmark", "buy_and_hold", None)
    combined_rule = Combined(trend_rule, carry_rule)
    combined = go("trend_and_carry", "not_headline", "signal", combined_rule)
    decisions = tuple(day for _month_end, day in schedule)
    oos_on = oos_start(decisions, config.oos_fraction)
    trend_report = _describe(trend, cash, basket, config, oos_on=oos_on, seed=config.bootstrap_seed)
    carry_report = _describe(carry, cash, basket, config, oos_on=oos_on, seed=config.bootstrap_seed + 1)
    basket_report = _describe(basket, cash, basket, config, oos_on=oos_on, seed=config.bootstrap_seed + 2)
    full_legs = (
        LegScore("trend", trend_report.excess_cagr, trend_report.sharpe_vs_cash),
        LegScore("carry", carry_report.excess_cagr, carry_report.sharpe_vs_cash),
    )
    oos_legs = (
        LegScore("trend", trend_report.oos_excess_cagr, trend_report.oos_sharpe_vs_cash),
        LegScore("carry", carry_report.oos_excess_cagr, carry_report.oos_sharpe_vs_cash),
    )
    blockers = combined_blockers(full_legs, oos_legs)
    headline: list[PathReport] = [trend_report, carry_report]
    if not blockers:
        promoted = go("trend_and_carry", "headline", "signal", combined_rule)
        headline.append(_describe(promoted, cash, basket, config, oos_on=oos_on, seed=config.bootstrap_seed + 3))

    grid: list[GridRow] = [_grid(trend, cash), _grid(carry, cash), _grid(basket, cash)]
    for months in config.trend_sensitivity_months:
        name = f"trend_sma_{months}"
        grid.append(_grid(go(name, "sensitivity", "signal", Trend(months=months, strategy_id=name)), cash))
    grid.append(
        _grid(
            go("trend_return_12m", "sensitivity", "signal", Trend(rule="return_12m", strategy_id="trend_return_12m")),
            cash,
        )
    )
    grid.append(
        _grid(
            go(
                "trend_inverse_vol",
                "sensitivity",
                "signal",
                Trend(
                    weighting="inverse_vol", vol_window=config.vol_window, vol_min=config.vol_min, strategy_id="trend_inverse_vol"
                ),
            ),
            cash,
        )
    )
    for k in config.carry_sensitivity_k:
        name = f"carry_k{k}"
        grid.append(_grid(go(name, "sensitivity", "signal", Carry(k=k, threshold=config.carry_threshold, strategy_id=name)), cash))
    for threshold in config.carry_sensitivity_threshold:
        label = format(threshold, "f")
        name = f"carry_threshold_{label}"
        grid.append(_grid(go(name, "sensitivity", "signal", Carry(k=config.carry_k, threshold=threshold, strategy_id=name)), cash))
    for cost_scenario in config.cost_scenarios:
        if cost_scenario.name == config.primary_cost_scenario:
            continue
        grid.append(
            _grid(go(f"trend_cost_{cost_scenario.name}", "sensitivity", "signal", trend_rule, cost_name=cost_scenario.name), cash)
        )
        grid.append(
            _grid(go(f"carry_cost_{cost_scenario.name}", "sensitivity", "signal", carry_rule, cost_name=cost_scenario.name), cash)
        )
    for deposit_scenario in config.deposit_scenarios:
        if deposit_scenario.name == config.primary_deposit_scenario:
            continue
        scenario_cash = go(f"gbp_cash_{deposit_scenario.name}", "benchmark", "cash", None, deposit_name=deposit_scenario.name)
        grid.append(
            _grid(
                go(
                    f"trend_deposit_{deposit_scenario.name}",
                    "sensitivity",
                    "signal",
                    trend_rule,
                    deposit_name=deposit_scenario.name,
                ),
                scenario_cash,
            )
        )
        grid.append(
            _grid(
                go(
                    f"carry_deposit_{deposit_scenario.name}",
                    "sensitivity",
                    "signal",
                    carry_rule,
                    deposit_name=deposit_scenario.name,
                ),
                scenario_cash,
            )
        )
    resource = files("sterling.research.fx").joinpath("data/fx-research-v1.json")
    digest = hashlib.sha256(resource.read_bytes()).hexdigest()
    stale = [f"{trend.name} {code} {days}" for code, days in trend.stale_foreign_days if days]
    stale.extend(f"{carry.name} {code} {days}" for code, days in carry.stale_foreign_days if days)
    text = render_report(
        banner=BANNER,
        spans=_spans(market),
        notes=config.source_notes,
        config_sha256=digest,
        sample_start=trend.rows[0].session,
        sample_end=trend.rows[-1].session,
        decisions=len(decisions),
        oos_start=oos_on,
        paths=tuple(headline),
        cash=_describe(cash, cash, basket, config, oos_on=oos_on, seed=config.bootstrap_seed + 4),
        basket=basket_report,
        grid=tuple(grid),
        walk_trend=walk_forward_excess(
            trend.rows,
            cash.rows,
            train_months=config.walk_train_months,
            test_months=config.walk_test_months,
            step_months=config.walk_step_months,
        ),
        walk_carry=walk_forward_excess(
            carry.rows,
            cash.rows,
            train_months=config.walk_train_months,
            test_months=config.walk_test_months,
            step_months=config.walk_step_months,
        ),
        combined_blockers=blockers,
        combined=_grid(combined, cash),
        stale_notes=tuple(stale),
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.txt").write_text(text, encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Research-only unleveraged FX deposit study. Does not trade.")
    parser.add_argument("--cache", type=Path, default=Path(".research-cache/fx/raw"))
    parser.add_argument("--output", type=Path, default=Path(".research-cache/fx/out"))
    args = parser.parse_args(argv)
    print(run_study(args.cache, args.output))
    return 0

"""Path statistics, the pre-registered split, and the block bootstrap.

The bootstrap resamples paired monthly returns in circular blocks. It is a
description of this sample, not a trading authorisation.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

from sterling.research.errors import ResearchError
from sterling.research.metrics import NavRow, performance, sample_stdev
from sterling.research.money import ZERO
from sterling.research.walkforward import DateSplit, assert_causal_split, walk_forward_splits

MONTHS = Decimal(12)


@dataclass(frozen=True, slots=True)
class BootstrapCI:
    point: Decimal
    low: Decimal
    high: Decimal
    draws: int
    seed: int
    block: int


def month_end_navs(rows: Sequence[NavRow]) -> tuple[NavRow, ...]:
    last: dict[tuple[int, int], NavRow] = {}
    for row in rows:
        last[(row.session.year, row.session.month)] = row
    return tuple(last[key] for key in sorted(last))


def monthly_returns(rows: Sequence[NavRow]) -> tuple[tuple[date, Decimal], ...]:
    ends = month_end_navs(rows)
    out: list[tuple[date, Decimal]] = []
    for previous, nxt in zip(ends, ends[1:], strict=False):
        if previous.nav_gbp <= 0:
            raise ResearchError("NAV must stay positive")
        out.append((nxt.session, nxt.nav_gbp / previous.nav_gbp - 1))
    return tuple(out)


def paired_monthly(strategy: Sequence[NavRow], benchmark: Sequence[NavRow]) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
    left = dict(monthly_returns(strategy))
    right = dict(monthly_returns(benchmark))
    shared = sorted(set(left) & set(right))
    if len(shared) < 2:
        raise ResearchError("paired monthly returns need at least two months")
    return tuple(left[day] for day in shared), tuple(right[day] for day in shared)


def sharpe_monthly(returns: Sequence[Decimal]) -> Decimal | None:
    if len(returns) < 2:
        return None
    deviation = sample_stdev(returns)
    if deviation == 0:
        return None
    mean = sum(returns, ZERO) / Decimal(len(returns))
    return mean / deviation * MONTHS.sqrt()


def annualised_mean_excess(strategy: Sequence[Decimal], benchmark: Sequence[Decimal]) -> Decimal:
    if len(strategy) != len(benchmark) or not strategy:
        raise ResearchError("excess return needs paired months")
    excess = [left - right for left, right in zip(strategy, benchmark, strict=True)]
    return sum(excess, ZERO) / Decimal(len(excess)) * MONTHS


def excess_cagr(strategy: Sequence[NavRow], benchmark: Sequence[NavRow]) -> Decimal | None:
    left = performance(strategy, total_cost_gbp=ZERO, total_turnover=ZERO).cagr
    right = performance(benchmark, total_cost_gbp=ZERO, total_turnover=ZERO).cagr
    if left is None or right is None:
        return None
    return left - right


def slice_rows(rows: Sequence[NavRow], start: date, end: date) -> tuple[NavRow, ...]:
    chosen = [row for row in rows if start <= row.session <= end]
    prior = [row for row in rows if row.session < start]
    if prior and (not chosen or prior[-1].session < chosen[0].session):
        chosen = [prior[-1], *chosen]
    return tuple(chosen)


def oos_start(decisions: Sequence[date], oos_fraction: Decimal) -> date:
    count = len(decisions)
    in_sample = int(((Decimal(1) - oos_fraction) * Decimal(count)).to_integral_value(rounding=ROUND_DOWN))
    if in_sample < 1 or in_sample >= count:
        raise ResearchError("out-of-sample split is empty")
    start = decisions[in_sample]
    train_end = decisions[in_sample - 1]
    if start <= train_end:
        raise ResearchError("out-of-sample decision is not after the in-sample decisions")
    return start


def worst_month(rows: Sequence[NavRow]) -> tuple[date, Decimal] | None:
    series = monthly_returns(rows)
    if not series:
        return None
    day, value = min(series, key=lambda item: item[1])
    return day, value


def worst_year(rows: Sequence[NavRow]) -> tuple[date, Decimal] | None:
    ends = month_end_navs(rows)
    worst: tuple[date, Decimal] | None = None
    for index in range(12, len(ends)):
        base = ends[index - 12].nav_gbp
        if base <= 0:
            continue
        value = ends[index].nav_gbp / base - 1
        if worst is None or value < worst[1]:
            worst = (ends[index].session, value)
    return worst


def recovery_text(rows: Sequence[NavRow]) -> str:
    path = performance(rows, total_cost_gbp=ZERO, total_turnover=ZERO)
    if path.max_drawdown_peak is None or path.max_drawdown_trough is None:
        return "n/a"
    peak_nav = next(row.nav_gbp for row in rows if row.session == path.max_drawdown_peak)
    recovered: date | None = None
    for row in rows:
        if row.session < path.max_drawdown_trough:
            continue
        if row.nav_gbp >= peak_nav and row.session > path.max_drawdown_peak:
            recovered = row.session
            break
    depth = (path.max_drawdown * Decimal(100)).quantize(Decimal("0.01"))
    base = (
        f"peak {path.max_drawdown_peak.isoformat()} trough {path.max_drawdown_trough.isoformat()} "
        f"depth {depth}% underwater_days {path.max_drawdown_underwater_days}"
    )
    if recovered is None:
        return base + " not recovered by the sample end"
    return base + f" recovered {recovered.isoformat()}"


def calendar_year_returns(rows: Sequence[NavRow]) -> tuple[tuple[int, Decimal, str], ...]:
    if len(rows) < 2:
        return ()
    last: dict[int, NavRow] = {}
    for row in rows:
        last[row.session.year] = row
    years = sorted(last)
    out: list[tuple[int, Decimal, str]] = []
    previous: NavRow | None = None
    for year in years:
        end = last[year]
        if previous is None:
            base = rows[0]
            flag = "partial"
        else:
            base = previous
            full = base.session.month == 12 and end.session.month == 12 and end.session.year == base.session.year + 1
            flag = "full" if full else "partial"
        if base.nav_gbp <= 0:
            raise ResearchError("NAV must stay positive")
        out.append((year, end.nav_gbp / base.nav_gbp - 1, flag))
        previous = end
    return tuple(out)


def window_return(rows: Sequence[NavRow], start: date, end: date) -> Decimal | None:
    chosen = slice_rows(rows, start, end)
    if len(chosen) < 2:
        return None
    if chosen[0].nav_gbp <= 0:
        return None
    return chosen[-1].nav_gbp / chosen[0].nav_gbp - 1


def _quantile(sorted_values: Sequence[Decimal], probability: Decimal) -> Decimal:
    if not sorted_values:
        raise ResearchError("bootstrap produced no finite draws")
    position = (Decimal(len(sorted_values) - 1) * probability).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    index = min(max(int(position), 0), len(sorted_values) - 1)
    return sorted_values[index]


def _resample(length: int, block: int, rng: random.Random) -> list[int]:
    indices: list[int] = []
    while len(indices) < length:
        start = rng.randrange(length)
        for step in range(block):
            indices.append((start + step) % length)
            if len(indices) == length:
                break
    return indices


def _bounds(draws: list[Decimal], point: Decimal, *, ci: Decimal, seed: int, block: int) -> BootstrapCI:
    if not Decimal(0) < ci < 1:
        raise ResearchError("bootstrap interval must sit between 0 and 1")
    tail = (Decimal(1) - ci) / Decimal(2)
    ordered = sorted(draws)
    return BootstrapCI(
        point=point,
        low=_quantile(ordered, tail),
        high=_quantile(ordered, Decimal(1) - tail),
        draws=len(draws),
        seed=seed,
        block=block,
    )


def bootstrap_mean_excess(
    strategy: Sequence[Decimal],
    benchmark: Sequence[Decimal],
    *,
    block: int,
    replications: int,
    seed: int,
    ci: Decimal,
) -> BootstrapCI:
    point = annualised_mean_excess(strategy, benchmark)
    if block < 1 or replications < 1:
        raise ResearchError("bootstrap block and replications must be at least 1")
    rng = random.Random(seed)
    length = len(strategy)
    draws: list[Decimal] = []
    for _ in range(replications):
        indices = _resample(length, block, rng)
        left = [strategy[index] for index in indices]
        right = [benchmark[index] for index in indices]
        draws.append(annualised_mean_excess(left, right))
    return _bounds(draws, point, ci=ci, seed=seed, block=block)


def bootstrap_sharpe_diff(
    strategy: Sequence[Decimal],
    benchmark: Sequence[Decimal],
    *,
    block: int,
    replications: int,
    seed: int,
    ci: Decimal,
) -> BootstrapCI:
    """Block bootstrap of the Sharpe of monthly excess return, not Sharpe minus Sharpe.

    A deposit benchmark has almost no volatility, so Sharpe(strategy) - Sharpe(cash)
    is dominated by an enormous cash Sharpe and is not the comparison used here.
    """
    if len(strategy) != len(benchmark) or len(strategy) < 2:
        raise ResearchError("Sharpe of excess needs paired months")
    excess = [left - right for left, right in zip(strategy, benchmark, strict=True)]
    point = sharpe_monthly(excess)
    if point is None:
        raise ResearchError("excess return has no finite Sharpe")
    rng = random.Random(seed)
    length = len(excess)
    draws: list[Decimal] = []
    for _ in range(replications):
        indices = _resample(length, block, rng)
        resampled = sharpe_monthly([excess[index] for index in indices])
        if resampled is None:
            continue
        draws.append(resampled)
    if len(draws) < replications // 2:
        raise ResearchError("too many bootstrap draws had zero volatility")
    return _bounds(draws, point, ci=ci, seed=seed, block=block)


@dataclass(frozen=True, slots=True)
class WalkWindow:
    split: DateSplit
    strategy_cagr: Decimal | None
    excess_cagr: Decimal | None


def walk_forward_excess(
    strategy: Sequence[NavRow],
    cash: Sequence[NavRow],
    *,
    train_months: int,
    test_months: int,
    step_months: int,
) -> tuple[WalkWindow, ...]:
    if not strategy:
        return ()
    splits = walk_forward_splits(
        strategy[0].session,
        strategy[-1].session,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
    )
    windows: list[WalkWindow] = []
    for split in splits:
        assert_causal_split(split)
        left = slice_rows(strategy, split.test_start, split.test_end)
        right = slice_rows(cash, split.test_start, split.test_end)
        if len(left) < 2 or len(right) < 2:
            continue
        windows.append(
            WalkWindow(split, performance(left, total_cost_gbp=ZERO, total_turnover=ZERO).cagr, excess_cagr(left, right))
        )
    return tuple(windows)


def median_decimal(values: Sequence[Decimal]) -> Decimal | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def advantage_failures(
    *,
    cagr: Decimal | None,
    excess: Decimal | None,
    sharpe_vs_cash: Decimal | None,
    oos_excess: Decimal | None,
    excess_ci_low: Decimal | None,
) -> tuple[str, ...]:
    """Mechanical bar for the written verdict. It was fixed before the run."""
    failures: list[str] = []
    if cagr is None or cagr <= 0:
        failures.append("full-sample net CAGR is not positive")
    if excess is None or excess <= 0:
        failures.append("full-sample CAGR does not exceed GBP cash")
    if sharpe_vs_cash is None or sharpe_vs_cash <= 0:
        failures.append("full-sample Sharpe versus GBP cash is not positive")
    if oos_excess is None or oos_excess <= 0:
        failures.append("out-of-sample CAGR does not exceed GBP cash")
    if excess_ci_low is None or excess_ci_low <= 0:
        failures.append("the 90% block-bootstrap interval for annualised mean excess return over GBP cash does not lie above zero")
    return tuple(failures)

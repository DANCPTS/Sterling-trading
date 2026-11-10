"""Performance statistics for a research path.

CAGR uses a 365.25-day year. Volatility, Sharpe, and Sortino use 252 and a
sample standard deviation. The risk-free rate is zero. Sharpe is mean daily
return divided by that deviation, annualised. Sortino uses the same mean and a
downside deviation of ``min(return, 0)`` with the sample denominator ``n - 1``.
Max-drawdown duration is the calendar gap from the peak of that episode to its
trough. Underwater days run from the peak until the NAV is back at the peak,
or to the last row if it has not recovered.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sterling.research.errors import ResearchError
from sterling.research.money import ZERO

TRADING_DAYS = Decimal(252)
YEAR_DAYS = Decimal("365.25")
METRIC_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class NavRow:
    session: date
    cash_gbp: Decimal
    market_value_gbp: Decimal
    nav_gbp: Decimal
    gross_exposure: Decimal
    cash_weight: Decimal
    holdings: tuple[tuple[str, Decimal], ...] = ()
    receivable_gbp: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class Performance:
    start_nav_gbp: Decimal
    end_nav_gbp: Decimal
    cagr: Decimal | None
    annualised_volatility: Decimal | None
    max_drawdown: Decimal
    max_drawdown_duration_days: int
    max_drawdown_underwater_days: int
    max_drawdown_peak: date | None
    max_drawdown_trough: date | None
    sharpe: Decimal | None
    sortino: Decimal | None
    calmar: Decimal | None
    monthly_win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    total_turnover: Decimal
    total_cost_gbp: Decimal
    total_cost_drag: Decimal | None
    annualised_cost_drag: Decimal | None
    average_cash_weight: Decimal
    fraction_days_in_cash: Decimal
    average_gross_exposure: Decimal
    observations: int


def _q(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(METRIC_QUANTUM)


def sample_stdev(values: Sequence[Decimal]) -> Decimal:
    count = len(values)
    if count < 2:
        return ZERO
    mean = sum(values, ZERO) / Decimal(count)
    accumulator = sum(((value - mean) ** 2 for value in values), ZERO)
    return (accumulator / Decimal(count - 1)).sqrt()


def sharpe_ratio(returns: Sequence[Decimal]) -> Decimal | None:
    """Annualised Sharpe with a zero risk-free rate and a sample deviation."""
    if len(returns) < 2:
        return None
    deviation = sample_stdev(returns)
    if deviation == 0:
        return None
    mean = sum(returns, ZERO) / Decimal(len(returns))
    return (mean / deviation * TRADING_DAYS.sqrt()).quantize(METRIC_QUANTUM)


def excess_sharpe(strategy_rows: Sequence[NavRow], funding_rows: Sequence[NavRow]) -> Decimal | None:
    """Sharpe of strategy daily return minus the funding path's daily return.

    The research review uses an XSTR total-return path as the funding series.
    That is a traded cash-proxy ETF, not the Bank of England SONIA print and
    not a guaranteed rate.
    """
    funding = {row.session: row.nav_gbp for row in funding_rows}
    excess: list[Decimal] = []
    previous_strategy: NavRow | None = None
    previous_funding: Decimal | None = None
    for row in strategy_rows:
        current_funding = funding.get(row.session)
        if (
            previous_strategy is not None
            and previous_funding is not None
            and current_funding is not None
            and previous_strategy.nav_gbp > 0
            and previous_funding > 0
        ):
            strategy_return = row.nav_gbp / previous_strategy.nav_gbp - 1
            funding_return = current_funding / previous_funding - 1
            excess.append(strategy_return - funding_return)
        previous_strategy = row
        if current_funding is not None:
            previous_funding = current_funding
    return sharpe_ratio(excess)


def daily_returns(rows: Sequence[NavRow]) -> list[Decimal]:
    returns: list[Decimal] = []
    for previous, nxt in zip(rows, rows[1:], strict=False):
        if previous.nav_gbp <= 0:
            raise ResearchError("NAV must stay positive")
        returns.append(nxt.nav_gbp / previous.nav_gbp - 1)
    return returns


def _cagr(start_nav: Decimal, end_nav: Decimal, start: date, end: date) -> Decimal | None:
    days = (end - start).days
    if days <= 0 or start_nav <= 0 or end_nav <= 0:
        return None
    return (end_nav / start_nav) ** (YEAR_DAYS / Decimal(days)) - 1


def _drawdown(rows: Sequence[NavRow]) -> tuple[Decimal, int, int, date | None, date | None]:
    peak = rows[0].nav_gbp
    peak_date = rows[0].session
    max_dd = ZERO
    dd_peak = peak_date
    dd_trough = peak_date
    dd_peak_nav = peak
    for row in rows:
        if row.nav_gbp >= peak:
            peak = row.nav_gbp
            peak_date = row.session
        if peak <= 0:
            continue
        drawdown = (peak - row.nav_gbp) / peak
        if drawdown > max_dd:
            max_dd = drawdown
            dd_peak = peak_date
            dd_trough = row.session
            dd_peak_nav = peak
    underwater = (rows[-1].session - dd_peak).days
    for row in rows:
        if row.session < dd_trough:
            continue
        if row.nav_gbp >= dd_peak_nav:
            underwater = (row.session - dd_peak).days
            break
    return max_dd, (dd_trough - dd_peak).days, underwater, dd_peak, dd_trough


def _monthly(rows: Sequence[NavRow]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    last_by_month: dict[tuple[int, int], NavRow] = {}
    for row in rows:
        last_by_month[(row.session.year, row.session.month)] = row
    ordered = [last_by_month[key] for key in sorted(last_by_month)]
    monthly: list[Decimal] = []
    for previous, nxt in zip(ordered, ordered[1:], strict=False):
        if previous.nav_gbp <= 0:
            raise ResearchError("NAV must stay positive")
        monthly.append(nxt.nav_gbp / previous.nav_gbp - 1)
    wins = [value for value in monthly if value > 0]
    losses = [value for value in monthly if value < 0]
    decisive = len(wins) + len(losses)
    if decisive == 0:
        return None, None, None
    win_rate = Decimal(len(wins)) / Decimal(decisive)
    average_win = sum(wins, ZERO) / Decimal(len(wins)) if wins else None
    average_loss = sum(losses, ZERO) / Decimal(len(losses)) if losses else None
    return win_rate, average_win, average_loss


def performance(
    rows: Sequence[NavRow],
    *,
    total_cost_gbp: Decimal,
    total_turnover: Decimal,
) -> Performance:
    if len(rows) < 2:
        raise ResearchError("metrics need at least two NAV rows")
    returns = daily_returns(rows)
    start = rows[0]
    end = rows[-1]
    growth = _cagr(start.nav_gbp, end.nav_gbp, start.session, end.session)
    deviation = sample_stdev(returns) if len(returns) >= 2 else ZERO
    mean = sum(returns, ZERO) / Decimal(len(returns)) if returns else ZERO
    volatility = deviation * TRADING_DAYS.sqrt() if len(returns) >= 2 else None
    sharpe = sharpe_ratio(returns)
    downside_acc = sum((min(value, ZERO) ** 2 for value in returns), ZERO)
    downside = (downside_acc / Decimal(len(returns) - 1)).sqrt() if len(returns) >= 2 else ZERO
    sortino = None if downside == 0 else mean / downside * TRADING_DAYS.sqrt()
    max_dd, duration, underwater, peak_date, trough_date = _drawdown(rows)
    calmar = None if max_dd == 0 or growth is None else growth / max_dd
    win_rate, average_win, average_loss = _monthly(rows)
    mean_nav = sum((row.nav_gbp for row in rows), ZERO) / Decimal(len(rows))
    days = (end.session - start.session).days
    total_drag = total_cost_gbp / start.nav_gbp if start.nav_gbp > 0 else None
    annualised_drag = None
    if mean_nav > 0 and days > 0:
        annualised_drag = total_cost_gbp / mean_nav * YEAR_DAYS / Decimal(days)
    cash_days = sum(1 for row in rows if row.gross_exposure == 0)
    return Performance(
        start_nav_gbp=start.nav_gbp,
        end_nav_gbp=end.nav_gbp,
        cagr=_q(growth),
        annualised_volatility=_q(volatility),
        max_drawdown=max_dd.quantize(METRIC_QUANTUM),
        max_drawdown_duration_days=duration,
        max_drawdown_underwater_days=underwater,
        max_drawdown_peak=peak_date,
        max_drawdown_trough=trough_date,
        sharpe=_q(sharpe),
        sortino=_q(sortino),
        calmar=_q(calmar),
        monthly_win_rate=_q(win_rate),
        average_win=_q(average_win),
        average_loss=_q(average_loss),
        total_turnover=total_turnover.quantize(METRIC_QUANTUM),
        total_cost_gbp=total_cost_gbp,
        total_cost_drag=_q(total_drag),
        annualised_cost_drag=_q(annualised_drag),
        average_cash_weight=_q(sum((row.cash_weight for row in rows), ZERO) / Decimal(len(rows))) or ZERO,
        fraction_days_in_cash=(Decimal(cash_days) / Decimal(len(rows))).quantize(METRIC_QUANTUM),
        average_gross_exposure=_q(sum((row.gross_exposure for row in rows), ZERO) / Decimal(len(rows))) or ZERO,
        observations=len(rows),
    )

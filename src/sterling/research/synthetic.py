"""Synthetic illustration. These paths are not market data and not a result."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sterling.research.bars import Bar
from sterling.research.calendar import WeekdayCalendar, xlon_weekday
from sterling.research.costs import CostModel
from sterling.research.engine import SimulationResult, simulate
from sterling.research.instruments import Instrument
from sterling.research.risk_config import ResearchLimits, load_research_limits
from sterling.research.strategies.trend import TrendFilter

# Weekday calendar only. 2024-04-01 is a Monday here. The real LSE was closed
# that day for Easter. This illustration does not claim exchange sessions.
ILLUSTRATION_START = date(2023, 10, 2)
ILLUSTRATION_END = date(2024, 5, 31)

EQUITY_LEVELS: dict[tuple[int, int], Decimal] = {
    (2023, 10): Decimal("100"),
    (2023, 11): Decimal("100"),
    (2023, 12): Decimal("100"),
    (2024, 1): Decimal("130"),
    (2024, 2): Decimal("130"),
    (2024, 3): Decimal("80"),
    (2024, 4): Decimal("80"),
    (2024, 5): Decimal("80"),
}


def illustration_instruments() -> tuple[Instrument, Instrument]:
    equity = Instrument(
        ticker="SYNTH-EQ",
        instrument_type="UCITS_ETF",
        product_class="synthetic",
        asset_class="equity",
        trading_currency="GBP",
        stamp_duty_exempt=True,
        distribution="accumulating",
        calendar_id="XLON_WEEKDAY",
        isin=None,
    )
    bond = Instrument(
        ticker="SYNTH-BOND",
        instrument_type="UCITS_ETF",
        product_class="synthetic",
        asset_class="bond",
        trading_currency="GBP",
        stamp_duty_exempt=True,
        distribution="accumulating",
        calendar_id="XLON_WEEKDAY",
        isin=None,
    )
    return equity, bond


def illustration_costs() -> CostModel:
    return CostModel(
        commission_flat_gbp=Decimal("1.00"),
        commission_bps=Decimal("0"),
        spread_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
        fx_conversion_bps=Decimal("0"),
        stamp_duty_rate=Decimal("0.005"),
    )


def level_bars(
    start: date,
    end: date,
    levels: dict[tuple[int, int], Decimal],
    *,
    calendar: WeekdayCalendar | None = None,
    quoted_in: str = "GBP",
) -> tuple[Bar, ...]:
    sessions = (calendar or xlon_weekday()).trading_days(start, end)
    bars: list[Bar] = []
    for session in sessions:
        level = levels[(session.year, session.month)]
        bars.append(
            Bar(
                session=session,
                open=level,
                high=level,
                low=level,
                close=level,
                volume=Decimal("1000"),
                quoted_in=quoted_in,
            )
        )
    return tuple(bars)


def illustration_bars(calendar: WeekdayCalendar | None = None) -> dict[str, tuple[Bar, ...]]:
    equity = dict(EQUITY_LEVELS)
    bond = {key: Decimal("100") for key in EQUITY_LEVELS}
    return {
        "SYNTH-EQ": level_bars(ILLUSTRATION_START, ILLUSTRATION_END, equity, calendar=calendar),
        "SYNTH-BOND": level_bars(ILLUSTRATION_START, ILLUSTRATION_END, bond, calendar=calendar),
    }


def run_illustration(
    *,
    limits: ResearchLimits | None = None,
    evaluate_start: date | None = None,
    evaluate_end: date | None = None,
    label: str = "SYNTHETIC ILLUSTRATION — not an official performance result",
) -> SimulationResult:
    """Trend filter on two synthetic ETFs.

    SYNTH-EQ is 100 for three months, 130 for January and February, then 80.
    The January month-end close is above its three-month average, so the
    proposal is 100% SYNTH-EQ. The proposed single-name cap clips that to 50%
    and the buy fills at the February open (130), not at a later price.
    The March close is below the average, so the book goes to cash. The sell
    fills at the April open (80). SYNTH-BOND never clears the trend filter.
    """
    active_limits = limits if limits is not None else load_research_limits()
    equity, bond = illustration_instruments()
    return simulate(
        instruments=(equity, bond),
        bars=illustration_bars(),
        calendar=xlon_weekday(),
        strategy=TrendFilter(lookback_months=3),
        limits=active_limits,
        cost_model=illustration_costs(),
        evaluate_start=evaluate_start,
        evaluate_end=evaluate_end,
        label=label,
    )

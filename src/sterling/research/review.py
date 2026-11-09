"""Pre-specified research review on a local price cache.

Nothing here is fetched. CSVs live in ``.research-cache/yahoo``, which is
gitignored. Results are labelled research review and are not official.
Parameters below were chosen before looking at the results.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from sterling.research.bars import Bar
from sterling.research.calendar import WeekdayCalendar, xlon
from sterling.research.costs import CostModel
from sterling.research.engine import SimulationResult, simulate
from sterling.research.errors import DataValidationError
from sterling.research.instruments import Candidate, Instrument, load_candidate_universe
from sterling.research.loaders import read_ohlcv_csv
from sterling.research.metrics import NavRow, excess_sharpe, performance
from sterling.research.money import ZERO
from sterling.research.risk_config import ResearchLimits, load_research_limits
from sterling.research.strategies.dual_momentum import DualMomentum
from sterling.research.strategies.trend import TrendFilter
from sterling.research.walkforward import walk_forward_splits

REVIEW_LABEL = "RESEARCH REVIEW - not official, pending owner approval of research limits"
STARTING_SET = ("VWRP", "CUKX", "IGLT", "AGBP", "SGLN", "XSTR")
OPTIONAL = ("CSP1", "ERNS")
PROXY_FOR_VWRP = "VWRL"

# Stated before any result. Not fitted to this sample.
PRIMARY_TREND_MONTHS = 10
PRIMARY_MOMENTUM_MONTHS = 12
PRIMARY_TOP_K = 2
TREND_SENSITIVITY = (8, 10, 12)
MOMENTUM_LOOKBACKS = (6, 12, 18)
MOMENTUM_TOP_K = (1, 2, 3)
WALK_TRAIN_MONTHS = 36
WALK_TEST_MONTHS = 12

# Ex-ante windows. Not chosen by inspecting strategy P&L.
COVID_CRASH = (date(2020, 2, 20), date(2020, 3, 23))
RATE_SHOCK_2022 = (date(2022, 1, 1), date(2022, 12, 31))
REGIME_UP = Decimal("0.10")
REGIME_DOWN = Decimal("-0.10")

# Research assumption, not a measured LSE quote and not a broker tariff.
# Stamp stays at 0.5% in the model and is not charged on these ETF/ETC lines.
REVIEW_COSTS = CostModel(
    commission_flat_gbp=Decimal("0"),
    commission_bps=Decimal("0"),
    spread_bps=Decimal("10"),
    slippage_bps=Decimal("5"),
    fx_conversion_bps=Decimal("0"),
    stamp_duty_rate=Decimal("0.005"),
)


def cache_directory() -> Path:
    return Path(".research-cache") / "yahoo"


def classify_trailing_equity(trailing_return: Decimal) -> str:
    """Up if the prior 12-month equity return is above 10%, down if below -10%."""
    if trailing_return > REGIME_UP:
        return "up"
    if trailing_return < REGIME_DOWN:
        return "down"
    return "sideways"


@dataclass(frozen=True, slots=True)
class AlignedSeries:
    bars: tuple[Bar, ...]
    dropped_non_sessions: tuple[date, ...]
    missing_sessions: tuple[date, ...]


def align_to_xlon(bars: Sequence[Bar], calendar: WeekdayCalendar) -> AlignedSeries:
    ordered = tuple(sorted(bars, key=lambda bar: bar.session))
    kept: list[Bar] = []
    dropped: list[date] = []
    for bar in ordered:
        if calendar.is_trading_day(bar.session):
            kept.append(bar)
        else:
            dropped.append(bar.session)
    if not kept:
        return AlignedSeries((), tuple(dropped), ())
    expected = set(calendar.trading_days(kept[0].session, kept[-1].session))
    present = {bar.session for bar in kept}
    missing = tuple(sorted(expected - present))
    return AlignedSeries(tuple(kept), tuple(dropped), missing)


def load_cache(tickers: Sequence[str], directory: Path | None = None) -> dict[str, tuple[Bar, ...]]:
    root = directory or cache_directory()
    loaded: dict[str, tuple[Bar, ...]] = {}
    for ticker in tickers:
        path = root / f"{ticker}.csv"
        if not path.is_file():
            raise DataValidationError(f"missing cached CSV {path}. Research review does not download it.")
        loaded[ticker] = read_ohlcv_csv(path)
    return loaded


def carry_prior_close(bars: Sequence[Bar], sessions: Sequence[date]) -> tuple[tuple[Bar, ...], tuple[date, ...]]:
    """Insert a flat prior-close bar on XLON sessions the file does not contain.

    This is a disclosed data repair for a missing vendor print. It is not a
    holiday and it does not use a future price.
    """
    by_day = {bar.session: bar for bar in bars}
    carried: list[date] = []
    for session in sessions:
        if session in by_day:
            continue
        earlier = [day for day in by_day if day < session]
        if not earlier:
            raise DataValidationError(f"no prior close to carry onto {session.isoformat()}")
        source = by_day[max(earlier)]
        level = source.close
        adjusted = source.adjusted_close
        by_day[session] = Bar(
            session=session,
            open=level,
            high=level,
            low=level,
            close=level,
            volume=ZERO,
            adjusted_close=adjusted,
            dividend=None,
            quoted_in=source.quoted_in,
        )
        carried.append(session)
    ordered = tuple(by_day[day] for day in sorted(by_day))
    return ordered, tuple(carried)


def common_bars(series: Mapping[str, Sequence[Bar]]) -> dict[str, tuple[Bar, ...]]:
    """Keep only sessions present for every ticker, preserving each file's quote unit."""
    sets = [{bar.session: bar for bar in bars} for bars in series.values()]
    shared = set(sets[0])
    for item in sets[1:]:
        shared &= set(item)
    ordered = sorted(shared)
    return {ticker: tuple(lookup[day] for day in ordered) for ticker, lookup in zip(series, sets, strict=True)}


def _instruments(tickers: Sequence[str], universe_rows: Mapping[str, Candidate]) -> tuple[Instrument, ...]:
    return tuple(universe_rows[ticker].as_instrument() for ticker in tickers)


def run_strategy(
    *,
    bars: Mapping[str, Sequence[Bar]],
    instruments: Sequence[Instrument],
    strategy: TrendFilter | DualMomentum,
    limits: ResearchLimits,
    label: str,
    include_benchmarks: bool,
) -> SimulationResult:
    return simulate(
        instruments=instruments,
        bars=bars,
        calendar=xlon(),
        strategy=strategy,
        limits=limits,
        cost_model=REVIEW_COSTS,
        label=label,
        include_benchmarks=include_benchmarks,
    )


def window_rows(rows: Sequence[NavRow], start: date, end: date) -> tuple[NavRow, ...]:
    return tuple(row for row in rows if start <= row.session <= end)


def total_return(rows: Sequence[NavRow]) -> Decimal | None:
    if len(rows) < 2 or rows[0].nav_gbp <= 0:
        return None
    return rows[-1].nav_gbp / rows[0].nav_gbp - 1


def cash_proxy_time(rows: Sequence[NavRow], ticker: str = "XSTR") -> tuple[Decimal, Decimal]:
    """Average weight, and the fraction of days that weight is at least 50%."""
    if not rows:
        return ZERO, ZERO
    weights = [dict(row.holdings).get(ticker, ZERO) for row in rows]
    average = sum(weights, ZERO) / Decimal(len(weights))
    heavy = sum(1 for weight in weights if weight >= Decimal("0.5"))
    return average, Decimal(heavy) / Decimal(len(weights))


def regime_returns(rows: Sequence[NavRow], equity_closes: Sequence[tuple[date, Decimal]]) -> dict[str, Decimal | None]:
    """Compound strategy returns in months labelled by the prior equity 12-month return."""
    month_ends: dict[tuple[int, int], tuple[date, Decimal]] = {}
    for session, price in equity_closes:
        month_ends[(session.year, session.month)] = (session, price)
    ordered = [month_ends[key] for key in sorted(month_ends)]
    labels: dict[tuple[int, int], str] = {}
    for index in range(12, len(ordered) - 1):
        start_price = ordered[index - 12][1]
        end_price = ordered[index][1]
        if start_price <= 0:
            continue
        trailing = end_price / start_price - 1
        next_month = ordered[index + 1][0]
        labels[(next_month.year, next_month.month)] = classify_trailing_equity(trailing)
    buckets: dict[str, list[Decimal]] = {"up": [], "down": [], "sideways": []}
    nav_by_month: dict[tuple[int, int], NavRow] = {}
    for row in rows:
        nav_by_month[(row.session.year, row.session.month)] = row
    months = [nav_by_month[key] for key in sorted(nav_by_month)]
    for previous, nxt in zip(months, months[1:], strict=False):
        label = labels.get((nxt.session.year, nxt.session.month))
        if label is None or previous.nav_gbp <= 0:
            continue
        buckets[label].append(nxt.nav_gbp / previous.nav_gbp - 1)
    compounded: dict[str, Decimal | None] = {}
    for name, values in buckets.items():
        if not values:
            compounded[name] = None
            continue
        level = Decimal(1)
        for value in values:
            level *= Decimal(1) + value
        compounded[name] = level - 1
    return compounded


def _fmt(value: Decimal | None, places: str = "0.0001") -> str:
    if value is None:
        return ""
    return format(value.quantize(Decimal(places)), "f")


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def summarise(result: SimulationResult, funding: Sequence[NavRow] | None, proxy: str = "XSTR") -> list[str]:
    metrics = result.metrics
    average_proxy, heavy_proxy = cash_proxy_time(result.nav, proxy)
    excess = excess_sharpe(result.nav, funding) if funding is not None else None
    lines = [
        f"{result.label}",
        f"end_nav {_money(metrics.end_nav_gbp)} cagr {_fmt(metrics.cagr)} vol {_fmt(metrics.annualised_volatility)} "
        f"max_dd {_fmt(metrics.max_drawdown)} sharpe_rf0 {_fmt(metrics.sharpe)} sharpe_vs_{proxy} {_fmt(excess)}",
        f"trades {len(result.fills)} costs_gbp {_money(metrics.total_cost_gbp)} "
        f"avg_{proxy.lower()} {_fmt(average_proxy)} days_at_least_half_{proxy.lower()} {_fmt(heavy_proxy)} "
        f"avg_literal_cash {_fmt(metrics.average_cash_weight)}",
    ]
    lines.append("benchmark end_nav cagr vol max_dd sharpe_rf0 trades_not_stored cost_gbp")
    lines.append(
        "strategy "
        + " ".join(
            [
                _money(metrics.end_nav_gbp),
                _fmt(metrics.cagr),
                _fmt(metrics.annualised_volatility),
                _fmt(metrics.max_drawdown),
                _fmt(metrics.sharpe),
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
                    _fmt(bench.cagr),
                    _fmt(bench.annualised_volatility),
                    _fmt(bench.max_drawdown),
                    _fmt(bench.sharpe),
                    _money(bench.total_cost_gbp),
                ]
            )
        )
    return lines


def condition_lines(result: SimulationResult, equity: Sequence[tuple[date, Decimal]]) -> list[str]:
    lines = ["market conditions (ex-ante; not fitted to the strategy):"]
    for name, bounds in (("covid_crash_2020-02-20_2020-03-23", COVID_CRASH), ("rate_inflation_2022", RATE_SHOCK_2022)):
        sliced = window_rows(result.nav, bounds[0], bounds[1])
        if len(sliced) < 2:
            lines.append(f"{name} outside this window")
            continue
        stats = performance(sliced, total_cost_gbp=ZERO, total_turnover=ZERO)
        lines.append(
            f"{name} {sliced[0].session.isoformat()}..{sliced[-1].session.isoformat()} "
            f"return {_fmt(total_return(sliced))} max_dd {_fmt(stats.max_drawdown)} vol {_fmt(stats.annualised_volatility)}"
        )
    regimes = regime_returns(result.nav, equity)
    lines.append(
        "regime compounded strategy return by prior 12m "
        + PROXY_EQUITY
        + f" (up > {_fmt(REGIME_UP)}, down < {_fmt(REGIME_DOWN)}): "
        + " ".join(f"{name} {_fmt(value)}" for name, value in regimes.items())
    )
    return lines


PROXY_EQUITY = "VWRP"


def _equity_closes(bars: Sequence[Bar]) -> tuple[tuple[date, Decimal], ...]:
    points: list[tuple[date, Decimal]] = []
    for bar in bars:
        price = bar.adjusted_close if bar.adjusted_close is not None else bar.close
        points.append((bar.session, price))
    return tuple(points)


def complete_window(
    series: Mapping[str, Sequence[Bar]],
    calendar: WeekdayCalendar,
) -> tuple[dict[str, tuple[Bar, ...]], tuple[date, ...]]:
    shared = common_bars(series)
    anchor = next(iter(shared))
    start = shared[anchor][0].session
    end = shared[anchor][-1].session
    present = {bar.session for bar in shared[anchor]}
    gaps = tuple(day for day in calendar.trading_days(start, end) if day not in present)
    if not gaps:
        return shared, ()
    repaired: dict[str, tuple[Bar, ...]] = {}
    for ticker, bars in shared.items():
        filled, _carried = carry_prior_close(bars, gaps)
        repaired[ticker] = tuple(bar for bar in filled if start <= bar.session <= end)
    return repaired, gaps


def format_review(directory: Path | None = None) -> str:
    universe = load_candidate_universe()
    by_ticker = {item.ticker: item for item in universe.instruments}
    limits = load_research_limits()
    root = directory or cache_directory()
    raw = load_cache((*STARTING_SET, *OPTIONAL, PROXY_FOR_VWRP), root)
    calendar = xlon()
    aligned: dict[str, AlignedSeries] = {ticker: align_to_xlon(bars, calendar) for ticker, bars in raw.items()}
    notes = [
        REVIEW_LABEL,
        "Parameters fixed in advance: trend 10-month SMA; dual momentum 12-month lookback, top 2, "
        "hurdle is XSTR's own trailing return. Sensitivity is reported and is not used to choose a rule.",
        "Costs: commission £0, spread 10 bps, slippage 5 bps, FX conversion 0, stamp not applied to ETF/ETC lines. "
        "Assumption, not a measured spread.",
        "Sharpe vs XSTR is the Sharpe of daily strategy return minus the XSTR total-return index. "
        "Where Yahoo adjusted close never leaves the raw close, distributions are reinvested. "
        "This is before dealing costs. It is not the Bank of England SONIA print and it is not guaranteed cash. "
        "sharpe_rf0 uses a zero risk-free rate.",
        "OCF is not deducted again. A material adjusted close is the total-return path. "
        "A flat adjusted close with a dividend column uses those distributions instead.",
        f"Limits status: {limits.status}",
    ]
    for ticker, item in aligned.items():
        if item.dropped_non_sessions or item.missing_sessions:
            notes.append(
                f"{ticker} dropped {len(item.dropped_non_sessions)} non-sessions, "
                f"missing {len(item.missing_sessions)} XLON sessions inside its own span"
            )
    shared, gaps = complete_window({ticker: aligned[ticker].bars for ticker in STARTING_SET}, calendar)
    start = shared["VWRP"][0].session
    end = shared["VWRP"][-1].session
    if gaps:
        notes.append(
            "Prior close carried onto XLON sessions with no common Yahoo print: "
            + ", ".join(day.isoformat() for day in gaps)
            + ". These are vendor gaps, not bank holidays."
        )
    notes.append(
        f"Common window {start.isoformat()} to {end.isoformat()} for {', '.join(STARTING_SET)}. "
        "It cannot start before VWRP's 2019 listing. Prices are the cached Yahoo sterling lines, "
        "pence-normalised, with adjusted close used for total return."
    )
    instruments = _instruments(STARTING_SET, by_ticker)
    trend = run_strategy(
        bars=shared,
        instruments=instruments,
        strategy=TrendFilter(lookback_months=PRIMARY_TREND_MONTHS, defensive="XSTR"),
        limits=limits,
        label=f"{REVIEW_LABEL} | trend_filter {PRIMARY_TREND_MONTHS}m defensive XSTR",
        include_benchmarks=True,
    )
    momentum = run_strategy(
        bars=shared,
        instruments=instruments,
        strategy=DualMomentum(lookback_months=PRIMARY_MOMENTUM_MONTHS, top_k=PRIMARY_TOP_K, defensive="XSTR"),
        limits=limits,
        label=f"{REVIEW_LABEL} | dual_momentum {PRIMARY_MOMENTUM_MONTHS}m top {PRIMARY_TOP_K} hurdle XSTR",
        include_benchmarks=True,
    )
    xstr_metrics_nav = _funding_nav(shared["XSTR"])
    lines = list(notes)
    equity = _equity_closes(shared["VWRP"])
    for result in (trend, momentum):
        lines.append("")
        lines.extend(summarise(result, xstr_metrics_nav, "XSTR"))
        lines.extend(condition_lines(result, equity))
        lines.extend(_walk_lines(result))
    lines.append("")
    lines.append("Sensitivity grid (not a selection). Primary rows are 10m trend and 12m top-2 momentum.")
    lines.extend(_sensitivity(shared, instruments, limits, xstr_metrics_nav))
    lines.extend(_proxy_section(aligned, by_ticker, limits))
    lines.extend(_optional_note(aligned))
    return "\n".join(lines) + "\n"


def _funding_nav(bars: Sequence[Bar]) -> tuple[NavRow, ...]:
    """Unit total-return NAV for the Sharpe funding series, before dealing costs.

    Uses the same prepare step as the simulator, so a flat adjusted close does
    not hide distributions. This is not a buy-and-hold after spreads.
    """
    from sterling.research.bars import prepare_series

    prepared = prepare_series(
        ticker="XSTR",
        distribution="distributing",
        trading_currency="GBP",
        bars=tuple(bars),
        calendar=xlon(),
    )
    cash = Decimal(1)
    shares = ZERO
    rows: list[NavRow] = []
    for index, bar in enumerate(prepared.bars):
        if index == 0:
            shares = cash / bar.open
            cash = ZERO
        if bar.dividend > 0 and shares > 0 and bar.close > 0:
            cash += shares * bar.dividend
            shares += cash / bar.close
            cash = ZERO
        nav = cash + shares * bar.close
        rows.append(
            NavRow(
                session=bar.session,
                cash_gbp=cash,
                market_value_gbp=shares * bar.close,
                nav_gbp=nav,
                gross_exposure=Decimal(1) if nav else ZERO,
                cash_weight=ZERO if nav == 0 else cash / nav,
            )
        )
    return tuple(rows)


def _walk_lines(result: SimulationResult) -> list[str]:
    if not result.nav:
        return []
    splits = walk_forward_splits(
        result.nav[0].session,
        result.nav[-1].session,
        train_months=WALK_TRAIN_MONTHS,
        test_months=WALK_TEST_MONTHS,
        step_months=WALK_TEST_MONTHS,
    )
    lines = [
        f"Walk-forward stated in advance: train {WALK_TRAIN_MONTHS} months, "
        f"test {WALK_TEST_MONTHS} months, step {WALK_TEST_MONTHS}. "
        "Parameters are not refitted. Figures are slices of the single pre-specified path."
    ]
    if not splits:
        lines.append("No complete walk-forward window inside this sample.")
        return lines
    for split in splits:
        sliced = window_rows(result.nav, split.test_start, split.test_end)
        if len(sliced) < 2:
            continue
        stats = performance(sliced, total_cost_gbp=ZERO, total_turnover=ZERO)
        lines.append(
            f"test {split.test_start.isoformat()}..{split.test_end.isoformat()} "
            f"return {_fmt(total_return(sliced))} max_dd {_fmt(stats.max_drawdown)} cagr {_fmt(stats.cagr)}"
        )
    return lines


def _sensitivity(
    bars: Mapping[str, Sequence[Bar]],
    instruments: Sequence[Instrument],
    limits: ResearchLimits,
    funding: Sequence[NavRow],
) -> list[str]:
    lines = ["rule cagr max_dd sharpe_rf0 sharpe_vs_xstr trades"]
    for months in TREND_SENSITIVITY:
        result = run_strategy(
            bars=bars,
            instruments=instruments,
            strategy=TrendFilter(lookback_months=months, defensive="XSTR"),
            limits=limits,
            label="sensitivity",
            include_benchmarks=False,
        )
        mark = " primary" if months == PRIMARY_TREND_MONTHS else ""
        lines.append(
            f"trend_{months}m{mark} {_fmt(result.metrics.cagr)} {_fmt(result.metrics.max_drawdown)} "
            f"{_fmt(result.metrics.sharpe)} {_fmt(excess_sharpe(result.nav, funding))} {len(result.fills)}"
        )
    for lookback in MOMENTUM_LOOKBACKS:
        for top_k in MOMENTUM_TOP_K:
            result = run_strategy(
                bars=bars,
                instruments=instruments,
                strategy=DualMomentum(lookback_months=lookback, top_k=top_k, defensive="XSTR"),
                limits=limits,
                label="sensitivity",
                include_benchmarks=False,
            )
            mark = " primary" if lookback == PRIMARY_MOMENTUM_MONTHS and top_k == PRIMARY_TOP_K else ""
            lines.append(
                f"momentum_{lookback}m_k{top_k}{mark} {_fmt(result.metrics.cagr)} {_fmt(result.metrics.max_drawdown)} "
                f"{_fmt(result.metrics.sharpe)} {_fmt(excess_sharpe(result.nav, funding))} {len(result.fills)}"
            )
    return lines


def _proxy_section(
    aligned: Mapping[str, AlignedSeries],
    by_ticker: Mapping[str, Candidate],
    limits: ResearchLimits,
) -> list[str]:
    names = ("VWRL", "CUKX", "IGLT", "AGBP", "SGLN", "XSTR")
    if any(not aligned[name].bars for name in names):
        return ["", "Secondary VWRL proxy window: cache is empty."]
    shared, gaps = complete_window({name: aligned[name].bars for name in names}, xlon())
    # VWRL is not in the draft universe. Build a research instrument from VWRP's
    # asset class and mark the proxy in the label only.
    vwrp = by_ticker["VWRP"].as_instrument()
    proxy = Instrument(
        ticker="VWRL",
        instrument_type=vwrp.instrument_type,
        product_class=vwrp.product_class,
        asset_class=vwrp.asset_class,
        trading_currency="GBP",
        stamp_duty_exempt=True,
        distribution="distributing",
        calendar_id="XLON",
        isin=None,
    )
    others = _instruments(("CUKX", "IGLT", "AGBP", "SGLN", "XSTR"), by_ticker)
    instruments = (proxy, *others)
    renamed = {"VWRL" if ticker == "VWRP" else ticker: bars for ticker, bars in shared.items()}
    # common_bars keys are the names above, already VWRL not VWRP.
    del renamed
    trend = run_strategy(
        bars=shared,
        instruments=instruments,
        strategy=TrendFilter(lookback_months=PRIMARY_TREND_MONTHS, defensive="XSTR"),
        limits=limits,
        label=(f"{REVIEW_LABEL} | SECONDARY proxy window, VWRL instead of VWRP (distributing share class, not the same fund line)"),
        include_benchmarks=False,
    )
    start = shared["VWRL"][0].session
    end = shared["VWRL"][-1].session
    gap_note = ""
    if gaps:
        gap_note = " Vendor gaps carried from the prior close: " + ", ".join(day.isoformat() for day in gaps) + "."
    return [
        "",
        f"Secondary window {start.isoformat()} to {end.isoformat()}.{gap_note} "
        "VWRL stands in for VWRP so the sample can start when AGBP exists rather than when VWRP lists. "
        "VWRL distributes; adjusted close is the total-return path. This is not the primary result.",
        f"trend_10m end_nav {_money(trend.metrics.end_nav_gbp)} cagr {_fmt(trend.metrics.cagr)} "
        f"max_dd {_fmt(trend.metrics.max_drawdown)} vol {_fmt(trend.metrics.annualised_volatility)} "
        f"sharpe_rf0 {_fmt(trend.metrics.sharpe)} trades {len(trend.fills)}",
    ]


def _optional_note(aligned: Mapping[str, AlignedSeries]) -> list[str]:
    available = [ticker for ticker in OPTIONAL if aligned[ticker].bars]
    return [
        "",
        "Optional comparisons "
        + ", ".join(available)
        + " are cached and are not in the primary six-fund book. CSP1 overlaps VWRP. ERNS is ultrashort credit, not SONIA cash.",
    ]


def main() -> None:
    print(format_review())


if __name__ == "__main__":
    main()

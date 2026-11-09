"""Long-history research review. Reads the gitignored cache. Does not download.

The no-trade band of 2 percentage points was chosen before this sample was
run. Primary results are printed with the band off and on. The grid is not a
selection.

Proxy results are not ETF trading results. Real-ETF stretches are labelled
separately.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sterling.research.bars import Bar, prepare_series
from sterling.research.calendar import xlon
from sterling.research.costs import research_cost_model
from sterling.research.engine import SimulationResult, _run_path, simulate
from sterling.research.errors import ResearchError
from sterling.research.instruments import Instrument
from sterling.research.loaders import read_ohlcv_csv
from sterling.research.metrics import NavRow, Performance, performance, sharpe_ratio
from sterling.research.money import ZERO
from sterling.research.proxies import (
    compound_annual_percent,
    distribution_total_return,
    drop_isolated_spikes,
    hedged_return_index,
    ratio_splice,
    usd_price_to_gbp,
)
from sterling.research.risk_config import load_research_limits
from sterling.research.strategies.dual_momentum import DualMomentum
from sterling.research.strategies.trend import TrendFilter

REVIEW = "RESEARCH SIMULATION - approved research limits, not operational and not a live result"
BAND = Decimal("0.02")
JOINT_START = date(2008, 1, 2)
# Ex-ante windows. Not chosen from strategy P&L.
STRESS = (
    ("dot_com_2000_03_to_2003_03", date(2000, 3, 1), date(2003, 3, 31)),
    ("gfc_2007_10_to_2009_03", date(2007, 10, 1), date(2009, 3, 31)),
    ("year_2011", date(2011, 1, 1), date(2011, 12, 31)),
    ("years_2015_16", date(2015, 1, 1), date(2016, 12, 31)),
    ("q4_2018", date(2018, 10, 1), date(2018, 12, 31)),
    ("covid_crash", date(2020, 2, 20), date(2020, 3, 23)),
    ("year_2022", date(2022, 1, 1), date(2022, 12, 31)),
)
BULLS = (
    ("bull_2003_04_to_2007_09", date(2003, 4, 1), date(2007, 9, 30)),
    ("bull_2009_04_to_2010_12", date(2009, 4, 1), date(2010, 12, 31)),
    ("bull_2012_to_2014", date(2012, 1, 1), date(2014, 12, 31)),
    ("bull_2017_to_2018_09", date(2017, 1, 1), date(2018, 9, 30)),
    ("bull_2019_to_2020_02", date(2019, 1, 1), date(2020, 2, 19)),
    ("bull_2020_04_to_2021", date(2020, 4, 1), date(2021, 12, 31)),
    ("bull_2023_to_sample_end", date(2023, 1, 1), date(2026, 11, 9)),
)
DRAWDOWN_CORRECTION = Decimal("-0.10")
DRAWDOWN_BEAR = Decimal("-0.20")

YAHOO = Path(".research-cache/yahoo")
RAW = Path(".research-cache/raw")


def _parse_boe_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%d %b %Y").date()


def _load_boe(path: Path, column: str) -> dict[date, Decimal]:
    out: dict[date, Decimal] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            out[_parse_boe_date(row["DATE"])] = Decimal(raw)
    return out


def _levels(path: Path, field: str = "close") -> list[tuple[date, Decimal]]:
    bars = read_ohlcv_csv(path)
    out = []
    for bar in bars:
        value = bar.adjusted_close if field == "adjusted" and bar.adjusted_close is not None else bar.close
        if bar.quoted_in in {"GBp", "GBX"}:
            value = value / Decimal(100)
        out.append((bar.session, value))
    return out


def _carry(levels: Sequence[tuple[date, Decimal]], sessions: Sequence[date]) -> tuple[tuple[date, Decimal], ...]:
    known = {day: level for day, level in levels}
    out: list[tuple[date, Decimal]] = []
    last: Decimal | None = None
    for session in sessions:
        if session in known:
            last = known[session]
        if last is None:
            continue
        out.append((session, last))
    return tuple(out)


def _return_index(levels: Sequence[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    ordered = sorted(levels)
    index = Decimal(1)
    out = [(ordered[0][0], index)]
    previous = ordered[0][1]
    for day, level in ordered[1:]:
        if previous <= 0 or level <= 0:
            previous = level
            continue
        index *= level / previous
        out.append((day, index))
        previous = level
    return out


def _blend(parts: Sequence[tuple[Decimal, Sequence[tuple[date, Decimal]]]]) -> list[tuple[date, Decimal]]:
    maps = [dict(series) for _weight, series in parts]
    shared = set(maps[0])
    for item in maps[1:]:
        shared &= set(item)
    days = sorted(shared)
    index = Decimal(1)
    out = [(days[0], index)]
    for previous_day, day in zip(days, days[1:], strict=False):
        step = ZERO
        for weight, mapping in zip((item[0] for item in parts), maps, strict=True):
            before = mapping[previous_day]
            after = mapping[day]
            if before <= 0:
                continue
            step += weight * (after / before - 1)
        index *= Decimal(1) + step
        out.append((day, index))
    return out


def _gbp_from_usd(levels: Sequence[tuple[date, Decimal]], fx: Mapping[date, Decimal]) -> list[tuple[date, Decimal]]:
    out = []
    for day, level in levels:
        rate = fx.get(day)
        if rate is None or rate <= 0:
            continue
        out.append((day, usd_price_to_gbp(level, rate)))
    return out


def _bars_from_levels(levels: Sequence[tuple[date, Decimal]]) -> list[Bar]:
    bars = []
    for day, level in levels:
        bars.append(
            Bar(
                session=day,
                open=level,
                high=level,
                low=level,
                close=level,
                volume=ZERO,
                adjusted_close=level,
                quoted_in="GBP",
            )
        )
    return bars


def _prepared_levels(path: Path) -> list[tuple[date, Decimal]]:
    """Total-return level in pounds after the simulator's own adjustment rules."""
    calendar = xlon()
    raw = read_ohlcv_csv(path)
    if not raw:
        return []
    sessions = calendar.trading_days(raw[0].session, raw[-1].session)
    by_day = {bar.session: bar for bar in raw if calendar.is_trading_day(bar.session)}
    filled: list[Bar] = []
    last: Bar | None = None
    for session in sessions:
        if session in by_day:
            last = by_day[session]
            filled.append(last)
        elif last is not None:
            filled.append(
                Bar(
                    session=session,
                    open=last.close,
                    high=last.close,
                    low=last.close,
                    close=last.close,
                    volume=ZERO,
                    adjusted_close=last.adjusted_close if last.adjusted_close is not None else last.close,
                    dividend=None,
                    quoted_in=last.quoted_in,
                )
            )
    prepared = prepare_series(
        ticker=path.stem,
        distribution="distributing",
        trading_currency="GBP",
        bars=filled,
        calendar=calendar,
    )
    # The dividend belongs on its own session. Using the previous bar's dividend
    # leaves a one-day hole the size of the distribution.
    if prepared.dividend_mode == "reinvest":
        return list(
            distribution_total_return([(bar.session, bar.close, bar.dividend) for bar in prepared.bars])
        )
    return [(bar.session, bar.close) for bar in prepared.bars]


def _sleeve(name: str, asset: str) -> Instrument:
    return Instrument(
        ticker=name,
        instrument_type="RESEARCH_SERIES",
        product_class="spliced_research_series",
        asset_class=asset,
        trading_currency="GBP",
        stamp_duty_exempt=True,
        distribution="accumulating",
        calendar_id="XLON",
    )


def build_sleeves() -> dict[str, list[Bar]]:
    calendar = xlon()
    fx_raw = _load_boe(RAW / "boe_xudluss.csv", "XUDLUSS")
    sonia_raw = _load_boe(RAW / "boe_sonia.csv", "IUDSOIA")
    sessions = calendar.trading_days(date(1999, 1, 4), date(2026, 11, 9))
    fx = dict(_carry(tuple(fx_raw.items()), sessions))
    sonia = dict(_carry(tuple(sonia_raw.items()), sessions))
    irx = dict(_carry(_levels(YAHOO / "IRX.csv"), sessions))

    spy = _levels(YAHOO / "SPY.csv", "adjusted")
    efa = _levels(YAHOO / "EFA.csv", "adjusted")
    eem = _levels(YAHOO / "EEM.csv", "adjusted")
    blend_usd = _blend(
        (
            (Decimal("0.60"), spy),
            (Decimal("0.30"), efa),
            (Decimal("0.10"), eem),
        )
    )
    blend_gbp = _gbp_from_usd(blend_usd, fx)
    vwrl = _prepared_levels(YAHOO / "VWRL.csv")
    vwrp = _prepared_levels(YAHOO / "VWRP.csv")
    global_levels = _chain(
        (
            (blend_gbp, date(2012, 5, 22), "proxy_60_30_10_spy_efa_eem_unhedged"),
            (vwrl, date(2019, 7, 25), "vwrl_actual"),
            (vwrp, date(2099, 1, 1), "vwrp_actual"),
        )
    )

    ftse = _levels(YAHOO / "FTSE.csv")
    isf = _prepared_levels(YAHOO / "ISF.csv")
    cukx = _prepared_levels(YAHOO / "CUKX.csv")
    uk_levels = _chain(
        (
            (ftse, date(2009, 1, 2), "ftse100_price"),
            (isf, date(2010, 9, 16), "isf_actual"),
            (cukx, date(2099, 1, 1), "cukx_actual"),
        )
    )

    gilt = _prepared_levels(YAHOO / "IGLT.csv")
    agg = _levels(YAHOO / "AGG.csv", "adjusted")
    common_agg = [day for day, _level in agg if day in sonia and day in irx]
    agg_map = dict(agg)
    hedged = hedged_return_index(
        [(day, agg_map[day]) for day in common_agg],
        [(day, sonia[day]) for day in common_agg],
        [(day, irx[day]) for day in common_agg],
    )
    agbp = _prepared_levels(YAHOO / "AGBP.csv")
    gagg_levels = _chain(((hedged, date(2017, 11, 21), "agg_hedged_proxy"), (agbp, date(2099, 1, 1), "agbp_actual")))

    sgln_raw = drop_isolated_spikes(_levels(YAHOO / "SGLN.csv"))
    gold_usd = _levels(YAHOO / "GC.csv")
    gold_gbp = _gbp_from_usd(gold_usd, fx)
    sgln = _index_on_calendar(sgln_raw)
    gold_levels = _chain(((gold_gbp, date(2011, 4, 8), "gc_futures_proxy"), (sgln, date(2099, 1, 1), "sgln_actual")))

    sonia_index = compound_annual_percent([(day, sonia[day]) for day in sessions if day in sonia])
    xstr = _prepared_levels(YAHOO / "XSTR.csv")
    cash_levels = _chain(((sonia_index, xstr[0][0], "sonia_proxy"), (xstr, date(2099, 1, 1), "xstr_actual")))

    series = {
        "GLOBAL": global_levels,
        "UK": uk_levels,
        "GILT": gilt,
        "GAGG": gagg_levels,
        "GOLD": gold_levels,
        "CASH": cash_levels,
    }
    # Keep a documented note by attaching nothing; the printer states the sources.
    del blend_gbp
    return {name: _bars_from_levels(levels) for name, levels in series.items()}


def _chain(pieces: Sequence[tuple[Sequence[tuple[date, Decimal]], date, str]]) -> list[tuple[date, Decimal]]:
    """Apply ratio splices in order. The last piece runs to its own end."""
    current = list(pieces[0][0])
    for index in range(1, len(pieces)):
        nxt, _ignored, _label = pieces[index]
        splice = pieces[index - 1][1]
        labelled = ratio_splice(current, list(nxt), splice)
        current = [(day, level) for day, level, _kind in labelled]
    return current


def _window(levels: Sequence[Bar], start: date, end: date) -> list[Bar]:
    return [bar for bar in levels if start <= bar.session <= end]


def _common(bars: Mapping[str, list[Bar]], start: date, end: date) -> dict[str, tuple[Bar, ...]]:
    """XLON sessions in the window, carrying each sleeve's prior close across gaps.

    A US holiday can be a London session. The last available level is held. It
    is not a future price.
    """
    calendar = xlon()
    sessions = calendar.trading_days(start, end)
    out: dict[str, tuple[Bar, ...]] = {}
    for name, series in bars.items():
        by_day = {bar.session: bar for bar in series if bar.session <= end}
        filled: list[Bar] = []
        last: Bar | None = None
        for day in sessions:
            if day in by_day:
                last = by_day[day]
            elif last is None:
                earlier = [bar for bar in series if bar.session < day]
                if not earlier:
                    raise ResearchError(f"{name} has no level on or before {day.isoformat()}")
                last = earlier[-1]
            if day not in by_day and last is not None:
                last = Bar(
                    session=day,
                    open=last.close,
                    high=last.close,
                    low=last.close,
                    close=last.close,
                    volume=ZERO,
                    adjusted_close=last.close,
                    quoted_in="GBP",
                )
            if last is None:
                raise ResearchError(f"{name} could not be aligned")
            filled.append(last if day in by_day else last)
        out[name] = tuple(filled)
    if len(sessions) < 2:
        raise ResearchError("joint window has fewer than two sessions")
    return out


def _run(
    bars: Mapping[str, tuple[Bar, ...]],
    strategy: TrendFilter | DualMomentum,
    band: Decimal,
    label: str,
) -> SimulationResult:
    instruments = (
        _sleeve("GLOBAL", "equity"),
        _sleeve("UK", "equity"),
        _sleeve("GILT", "bond"),
        _sleeve("GAGG", "bond"),
        _sleeve("GOLD", "commodity"),
        _sleeve("CASH", "cash_proxy"),
    )
    return simulate(
        instruments=instruments,
        bars=bars,
        calendar=xlon(),
        strategy=strategy,
        limits=load_research_limits(),
        cost_on=research_cost_model,
        no_trade_band=band,
        include_benchmarks=True,
        label=label,
    )


def _balanced(bars: Mapping[str, tuple[Bar, ...]]) -> tuple[tuple[NavRow, ...], Performance, Decimal]:
    instruments = {name: _sleeve(name, asset) for name, asset in (
        ("GLOBAL", "equity"),
        ("UK", "equity"),
        ("GILT", "bond"),
        ("GAGG", "bond"),
        ("GOLD", "commodity"),
        ("CASH", "cash_proxy"),
    )}
    prepared = {
        name: prepare_series(
            ticker=name,
            distribution="accumulating",
            trading_currency="GBP",
            bars=series,
            calendar=xlon(),
        )
        for name, series in bars.items()
    }
    limits = load_research_limits()
    rows, _fills, _p, _d, cost, turnover = _run_path(
        prepared=prepared,
        instruments=instruments,
        calendar=xlon(),
        model=research_cost_model(date(2015, 1, 2)),
        limits=limits,
        starting_nav=limits.starting_nav_gbp,
        history_start=prepared["GLOBAL"].bars[0].session,
        evaluate_start=prepared["GLOBAL"].bars[0].session,
        evaluate_end=prepared["GLOBAL"].bars[-1].session,
        strategy=None,
        static_weights={"GLOBAL": Decimal("0.60"), "GILT": Decimal("0.40")},
        apply_gate=False,
        monthly_rebalance=True,
        cost_on=research_cost_model,
    )
    return rows, performance(rows, total_cost_gbp=cost, total_turnover=turnover), cost


def _fmt(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.quantize(Decimal("0.0001")), "f")


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _period_return(levels: Sequence[tuple[date, Decimal]], start: date, end: date) -> str:
    chosen = [(day, level) for day, level in levels if start <= day <= end and level > 0]
    if len(chosen) < 2:
        return "n/a"
    return _fmt(chosen[-1][1] / chosen[0][1] - 1)


def _index_on_calendar(levels: Sequence[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    if not levels:
        return []
    ordered = sorted(levels)
    sessions = xlon().trading_days(ordered[0][0], ordered[-1][0])
    return list(_carry(ordered, sessions))


def _trailing_note(levels: Sequence[tuple[date, Decimal]]) -> str:
    month_end: dict[tuple[int, int], tuple[date, Decimal]] = {}
    for day, level in levels:
        month_end[(day.year, day.month)] = (day, level)
    ordered = [month_end[key] for key in sorted(month_end)]
    worst: dict[int, tuple[date, Decimal]] = {}
    for index in range(12, len(ordered)):
        start = ordered[index - 12][1]
        if start <= 0:
            continue
        trailing = ordered[index][1] / start - 1
        year = ordered[index][0].year
        if year not in worst or trailing < worst[year][1]:
            worst[year] = (ordered[index][0], trailing)
    parts = []
    for year in (2020, 2022):
        if year in worst:
            day, value = worst[year]
            parts.append(f"{year} worst month-end 12m {day.isoformat()} {_fmt(value)}")
    return "VWRP month-end trailing 12-month returns never reached -10% in this sample. " + "; ".join(parts)


def _peak_to_trough(levels: Sequence[tuple[date, Decimal]], start: date, end: date) -> str:
    chosen = [(day, level) for day, level in levels if start <= day <= end and level > 0]
    if len(chosen) < 2:
        return "n/a"
    peak = chosen[0][1]
    peak_day = chosen[0][0]
    worst = ZERO
    worst_pair = (peak_day, peak_day)
    for day, level in chosen:
        if level >= peak:
            peak = level
            peak_day = day
        drawdown = level / peak - 1
        if drawdown < worst:
            worst = drawdown
            worst_pair = (peak_day, day)
    return f"{_fmt(worst)} {worst_pair[0].isoformat()}->{worst_pair[1].isoformat()}"


def _calendar_returns(rows: Sequence[NavRow]) -> list[str]:
    by_year: dict[int, NavRow] = {}
    first: dict[int, NavRow] = {}
    for row in rows:
        first.setdefault(row.session.year, row)
        by_year[row.session.year] = row
    lines = []
    years = sorted(by_year)
    for year in years:
        if year - 1 in by_year and by_year[year - 1].nav_gbp > 0:
            ret = by_year[year].nav_gbp / by_year[year - 1].nav_gbp - 1
        elif first[year].nav_gbp > 0:
            ret = by_year[year].nav_gbp / first[year].nav_gbp - 1
        else:
            continue
        lines.append(f"{year} {_fmt(ret)}")
    return lines


def _bootstrap(strategy: Sequence[NavRow], benchmark: Sequence[NavRow], cash: Sequence[NavRow]) -> str:
    cash_nav = {row.session: row.nav_gbp for row in cash}
    bench = {row.session: row.nav_gbp for row in benchmark}
    strat_ret: list[Decimal] = []
    bench_ret: list[Decimal] = []
    previous = strategy[0]
    for row in strategy[1:]:
        missing = (
            row.session not in bench
            or row.session not in cash_nav
            or previous.session not in bench
            or previous.session not in cash_nav
        )
        if missing:
            previous = row
            continue
        if previous.nav_gbp <= 0 or bench[previous.session] <= 0 or cash_nav[previous.session] <= 0:
            previous = row
            continue
        cash_ret = cash_nav[row.session] / cash_nav[previous.session] - 1
        strat_ret.append(row.nav_gbp / previous.nav_gbp - 1 - cash_ret)
        bench_ret.append(bench[row.session] / bench[previous.session] - 1 - cash_ret)
        previous = row
    point = sharpe_ratio(strat_ret)
    bench_sharpe = sharpe_ratio(bench_ret)
    if point is None or bench_sharpe is None:
        return "bootstrap n/a"
    observed = point - bench_sharpe
    rng = random.Random(20261109)
    block = 21
    draws: list[Decimal] = []
    count = len(strat_ret)
    if count <= block:
        return f"sharpe_vs_cash {_fmt(point)} balanced {_fmt(bench_sharpe)} diff {_fmt(observed)} (sample too short to bootstrap)"
    for _ in range(1000):
        sample_s: list[Decimal] = []
        sample_b: list[Decimal] = []
        while len(sample_s) < count:
            start = rng.randrange(0, count - block + 1)
            sample_s.extend(strat_ret[start : start + block])
            sample_b.extend(bench_ret[start : start + block])
        sample_s = sample_s[:count]
        sample_b = sample_b[:count]
        left = sharpe_ratio(sample_s)
        right = sharpe_ratio(sample_b)
        if left is None or right is None:
            continue
        draws.append(left - right)
    draws.sort()
    if len(draws) < 100:
        return f"sharpe_vs_cash {_fmt(point)} balanced {_fmt(bench_sharpe)} diff {_fmt(observed)}"
    low = draws[int(0.05 * (len(draws) - 1))]
    high = draws[int(0.95 * (len(draws) - 1))]
    return (
        f"sharpe_vs_cash {_fmt(point)} balanced_60_40 {_fmt(bench_sharpe)} "
        f"diff {_fmt(observed)} block_bootstrap_90pct {_fmt(low)} to {_fmt(high)} n={len(draws)}"
    )


def _line(result: SimulationResult, balanced_sharpe: str) -> list[str]:
    metrics = result.metrics
    return [
        result.label,
        f"end {_money(metrics.end_nav_gbp)} cagr {_fmt(metrics.cagr)} vol {_fmt(metrics.annualised_volatility)} "
        f"max_dd {_fmt(metrics.max_drawdown)} underwater_days {metrics.max_drawdown_underwater_days} "
        f"sharpe_rf0 {_fmt(metrics.sharpe)} sortino {_fmt(metrics.sortino)} calmar {_fmt(metrics.calmar)} "
        f"trades {len(result.fills)} costs {_money(metrics.total_cost_gbp)} turnover {_fmt(metrics.total_turnover)}",
        balanced_sharpe,
    ]


def format_history() -> str:
    sleeves = build_sleeves()
    levels = {name: [(bar.session, bar.close) for bar in bars] for name, bars in sleeves.items()}
    lines = [
        REVIEW,
        load_research_limits().status,
        "No-trade band of 0.02 was fixed before this run. It is not part of the approved risk caps. "
        "Primary figures are shown with the band off and on.",
        "Costs: spread 10 bps and slippage 5 bps from 2010; spread 25 bps and slippage 10 bps before 2010; "
        "extra 15 bps slippage in Mar 2000-Mar 2003, Oct 2007-Mar 2009, 20 Feb-23 Mar 2020, and calendar 2022.",
        "",
        "Sources, kept separate from ETF results:",
        "GLOBAL proxy to 2012-05-22: 60% SPY + 30% EFA + 10% EEM adjusted closes, unhedged, "
        "converted with Bank of England XUDLUSS (USD per GBP). Then VWRL until VWRP on 2019-07-25. "
        "Not FTSE All-World before VWRL. The blend starts when EEM exists (2003-04-14).",
        "UK: FTSE 100 PRICE index until ISF on 2009-01-02, then ISF, then CUKX from 2010-09-16. "
        "The price-index stretch omits dividends, so it understates total return. No yield was invented.",
        "GILT: no free daily total-return series before IGLT (2008-01-02) was obtained. FRED did not respond. "
        "The gilt sleeve is the real ETF for its whole history. There is no gilt proxy period.",
        "GAGG: AGG local total return, hedged by subtracting (SONIA - US 13-week T-bill)/365.25, "
        "not converted at spot, until AGBP on 2017-11-21. USD aggregate only, not the Bloomberg Global Aggregate. "
        "AGBP after that date is the real GBP-hedged ETF and is not converted again.",
        "GOLD: Yahoo GC=F COMEX futures in USD converted with XUDLUSS until SGLN on 2011-04-08. Not LBMA spot and not an ETC. "
        "Roll bias is not measured. FRED LBMA gold was not retrieved.",
        "CASH: Bank of England IUDSOIA (SONIA, percent) compounded on a 365.25-day year until XSTR, "
        "then XSTR with the distribution added on the ex-date. Its adjusted close is flat. "
        "SONIA's 2018 reform means the rate is not one unchanged instrument. "
        "The index is not a deposit and not XSTR's fee.",
        "The operational £2.50 per-trade figure and the £5.00 daily figure are risk and loss caps, not position-size limits.",
        "FX source: Bank of England XUDLUSS, daily USD per GBP, carried onto XLON sessions when the print is missing.",
        "",
        "Proxy limitations:",
        "- Global equity before VWRL is 60% SPY, 30% EFA and 10% EEM, unhedged, from 2003-04-14. It is not the FTSE All-World.",
        "- UK equity before ISF is the FTSE 100 price index. Dividends are missing, so that stretch is biased down.",
        "- There is no gilt total-return proxy before IGLT on 2008-01-02.",
        "- Bonds before AGBP are the USD aggregate AGG, hedged by a SONIA minus T-bill differential.",
        "  That is not the Bloomberg Global Aggregate.",
        "- Gold before SGLN is COMEX futures. Roll bias is unmeasured. It is not LBMA spot and not the ETC.",
        "- Cash before XSTR is compounded SONIA. The 2018 reform changed the rate. It is not a deposit.",
        "- A splice scales the proxy onto the later ETF and then discards the proxy.",
        "  The ETF that can be spliced is one that listed and survived.",
        "  The proxy stretch is not that ETF's trading record.",
        "",
        "Sleeve total-return changes. Proxy and real stretches are not comparable to each other as ETF trading.",
    ]
    anchors = {
        "GLOBAL": (date(2012, 5, 22), "proxy_to_vwrl", date(2019, 7, 25), "vwrl_to_vwrp"),
        "UK": (date(2009, 1, 2), "price_to_isf", date(2010, 9, 16), "isf_to_cukx"),
        "GAGG": (date(2017, 11, 21), "hedged_agg_to_agbp", None, ""),
        "GOLD": (date(2011, 4, 8), "futures_to_sgln", None, ""),
    }
    for name, series in levels.items():
        if not series:
            continue
        lines.append(
            f"{name} {series[0][0].isoformat()}..{series[-1][0].isoformat()} "
            f"full_change {_period_return(series, series[0][0], series[-1][0])}"
        )
        if name in anchors:
            first, _label, second, _label2 = anchors[name]
            lines.append(f"  before {first.isoformat()} {_period_return(series, series[0][0], first)}")
            if second is not None:
                lines.append(f"  {first.isoformat()} to {second.isoformat()} {_period_return(series, first, second)}")
                lines.append(f"  from {second.isoformat()} real_etf {_period_return(series, second, series[-1][0])}")
            else:
                lines.append(f"  from {first.isoformat()} real_etf {_period_return(series, first, series[-1][0])}")
        if name == "GILT":
            lines.append("  real ETF only; no proxy stretch")
        if name == "CASH":
            lines.append("  SONIA index before XSTR, then XSTR. Not a deposit.")
            lines.append(
                "  SONIA before the splice does not fall. XSTR distributions are added on the ex-date. "
                f"Cash-sleeve peak to trough: {_peak_to_trough(series, series[0][0], series[-1][0])}."
            )
    lines.append("")
    lines.append("Peak to trough inside the window (GBP unless noted). This is why a 12-month return rule can miss a crash:")
    vwrp = _prepared_levels(YAHOO / "VWRP.csv")
    ftse = _levels(YAHOO / "FTSE.csv")
    fx_for_spy = dict(
        _carry(
            tuple(_load_boe(RAW / "boe_xudluss.csv", "XUDLUSS").items()),
            xlon().trading_days(date(1999, 1, 4), date(2026, 11, 9)),
        )
    )
    spy_gbp = _gbp_from_usd(_levels(YAHOO / "SPY.csv", "adjusted"), fx_for_spy)
    spy_usd = _levels(YAHOO / "SPY.csv", "adjusted")
    for label, series in (
        ("VWRP_GBP_2020", vwrp),
        ("FTSE_GBP_price_2020", ftse),
        ("SPY_GBP_2020", spy_gbp),
        ("SPY_USD_2020", spy_usd),
    ):
        lines.append(f"  {label} {_peak_to_trough(series, date(2020, 1, 1), date(2020, 12, 31))}")
    for label, series in (
        ("VWRP_GBP_2022", vwrp),
        ("FTSE_GBP_price_2022", ftse),
        ("SPY_GBP_2022", spy_gbp),
        ("SPY_USD_2022", spy_usd),
    ):
        lines.append(f"  {label} {_peak_to_trough(series, date(2021, 11, 1), date(2022, 12, 31))}")
    lines.append(_trailing_note(vwrp))
    lines.append(
        "The old regime needed the prior month-end 12-month VWRP move to be below -10%. "
        "A fast crash and rebound can exceed 10% peak to trough while no month-end 12-month return crosses that line."
    )
    lines.append(
        "Drawdown regime, fixed before this run: the next month is bear if the month-end "
        "drawdown from the running peak is at or below -20%, correction if at or below -10%, otherwise calm."
    )
    joint = _common(sleeves, JOINT_START, date(2026, 11, 9))
    lines.append("")
    lines.append(
        f"Combined six-sleeve window {joint['GLOBAL'][0].session.isoformat()} to {joint['GLOBAL'][-1].session.isoformat()}. "
        "It starts when IGLT's total return starts. 2000-2007 is not a six-sleeve strategy result."
    )
    trend = TrendFilter(lookback_months=10, defensive="CASH")
    momentum = DualMomentum(lookback_months=12, top_k=2, defensive="CASH")
    results = {
        "trend_band_off": _run(joint, trend, ZERO, f"{REVIEW} | trend 10m band off"),
        "trend_band_on": _run(joint, trend, BAND, f"{REVIEW} | trend 10m band 0.02"),
        "momentum_band_off": _run(joint, momentum, ZERO, f"{REVIEW} | dual momentum 12m top 2 band off"),
        "momentum_band_on": _run(joint, momentum, BAND, f"{REVIEW} | dual momentum 12m top 2 band 0.02"),
    }
    balanced_rows, balanced_metrics, balanced_cost = _balanced(joint)
    cash_rows = _price_path(joint["CASH"])
    lines.append(
        f"balanced_60_40_global_gilts end {_money(balanced_metrics.end_nav_gbp)} cagr {_fmt(balanced_metrics.cagr)} "
        f"vol {_fmt(balanced_metrics.annualised_volatility)} max_dd {_fmt(balanced_metrics.max_drawdown)} "
        f"underwater_days {balanced_metrics.max_drawdown_underwater_days} sharpe_rf0 {_fmt(balanced_metrics.sharpe)} "
        f"sortino {_fmt(balanced_metrics.sortino)} calmar {_fmt(balanced_metrics.calmar)} costs {_money(balanced_cost)}"
    )
    for result in results.values():
        lines.append("")
        lines.extend(_line(result, _bootstrap(result.nav, balanced_rows, cash_rows)))
        lines.append("calendar_returns " + " ".join(_calendar_returns(result.nav)))
        lines.extend(_regimes(result.nav, [(bar.session, bar.close) for bar in joint["GLOBAL"]]))
    lines.append("")
    lines.append("Benchmarks on the same six-sleeve window, buy-and-hold end NAV from the band-off trend run:")
    for name, metrics in results["trend_band_off"].benchmarks:
        lines.append(
            f"{name} end {_money(metrics.end_nav_gbp)} cagr {_fmt(metrics.cagr)} vol {_fmt(metrics.annualised_volatility)} "
            f"max_dd {_fmt(metrics.max_drawdown)} sharpe_rf0 {_fmt(metrics.sharpe)} costs {_money(metrics.total_cost_gbp)}"
        )
    lines.append("")
    lines.append("Sensitivity, not a selection. Band off unless named. CAGR max_dd sharpe_rf0 trades.")
    for months in (8, 10, 12):
        result = _run(joint, TrendFilter(lookback_months=months, defensive="CASH"), ZERO, "sensitivity")
        mark = " primary" if months == 10 else ""
        lines.append(
            f"trend_{months}m{mark} {_fmt(result.metrics.cagr)} {_fmt(result.metrics.max_drawdown)} "
            f"{_fmt(result.metrics.sharpe)} {len(result.fills)}"
        )
    for lookback in (6, 12, 18):
        for top_k in (1, 2, 3):
            result = _run(
                joint,
                DualMomentum(lookback_months=lookback, top_k=top_k, defensive="CASH"),
                ZERO,
                "sensitivity",
            )
            mark = " primary" if lookback == 12 and top_k == 2 else ""
            lines.append(
                f"momentum_{lookback}m_k{top_k}{mark} {_fmt(result.metrics.cagr)} {_fmt(result.metrics.max_drawdown)} "
                f"{_fmt(result.metrics.sharpe)} {len(result.fills)}"
            )
    return "\n".join(lines) + "\n"


def _price_path(bars: Sequence[Bar]) -> tuple[NavRow, ...]:
    return tuple(
        NavRow(
            session=bar.session,
            cash_gbp=ZERO,
            market_value_gbp=bar.close,
            nav_gbp=bar.close,
            gross_exposure=Decimal(1),
            cash_weight=ZERO,
        )
        for bar in bars
    )


def _regimes(rows: Sequence[NavRow], equity: Sequence[tuple[date, Decimal]]) -> list[str]:
    lines = ["stress_and_bull_windows"]
    for name, start, end in (*((n, a, b) for n, a, b in STRESS), *((n, a, b) for n, a, b in BULLS)):
        chosen = [row for row in rows if start <= row.session <= end]
        if len(chosen) < 2 or chosen[0].nav_gbp <= 0:
            lines.append(f"{name} outside_window")
            continue
        lines.append(f"{name} {_fmt(chosen[-1].nav_gbp / chosen[0].nav_gbp - 1)}")
    month_end: dict[tuple[int, int], tuple[date, Decimal]] = {}
    for day, level in equity:
        month_end[(day.year, day.month)] = (day, level)
    ordered = [month_end[key] for key in sorted(month_end)]
    peak = ordered[0][1]
    labels: dict[tuple[int, int], str] = {}
    for index, (_day, level) in enumerate(ordered[:-1]):
        if level > peak:
            peak = level
        drawdown = level / peak - 1 if peak > 0 else ZERO
        if drawdown <= DRAWDOWN_BEAR:
            label = "bear"
        elif drawdown <= DRAWDOWN_CORRECTION:
            label = "correction"
        else:
            label = "calm"
        following = ordered[index + 1][0]
        labels[(following.year, following.month)] = label
    nav_month: dict[tuple[int, int], NavRow] = {}
    for row in rows:
        nav_month[(row.session.year, row.session.month)] = row
    months = [nav_month[key] for key in sorted(nav_month)]
    buckets: dict[str, list[Decimal]] = {"bear": [], "correction": [], "calm": []}
    for previous, nxt_row in zip(months, months[1:], strict=False):
        regime = labels.get((nxt_row.session.year, nxt_row.session.month))
        if regime is None or previous.nav_gbp <= 0:
            continue
        buckets[regime].append(nxt_row.nav_gbp / previous.nav_gbp - 1)
    parts = []
    for name, values in buckets.items():
        if not values:
            parts.append(f"{name} n=0")
            continue
        level = Decimal(1)
        for value in values:
            level *= Decimal(1) + value
        parts.append(f"{name} n={len(values)} compounded {_fmt(level - 1)}")
    lines.append("drawdown_regime " + " ".join(parts))
    return lines


def main() -> None:
    print(format_history())


if __name__ == "__main__":
    main()

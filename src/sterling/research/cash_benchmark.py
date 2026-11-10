"""Sterling cash benchmark for research, including the XSTR distribution calendar.

``total_return_index`` counts a distribution on the ex-date, which is the
holder's NAV once the receivable is included. It does not wait for the payment
date and it does not pay dealing costs. The simulator reinvests only on the
session after payment. Another branch can call ``write_cash_benchmark`` and
read ``.research-cache/exports/cash-benchmark.csv``. The file is not committed.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sterling.research.bars import Bar
from sterling.research.calendar import xlon
from sterling.research.distributions import xstr_pay_dates
from sterling.research.errors import ResearchError
from sterling.research.loaders import read_ohlcv_csv
from sterling.research.money import ZERO
from sterling.research.proxies import compound_annual_percent, distribution_total_return

YAHOO = Path(".research-cache/yahoo")
RAW = Path(".research-cache/raw")
EXPORT = Path(".research-cache/exports/cash-benchmark.csv")


def _boe_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%d %b %Y").date()


def _sonia() -> dict[date, Decimal]:
    path = RAW / "boe_sonia.csv"
    if not path.is_file():
        raise ResearchError("cash benchmark needs .research-cache/raw/boe_sonia.csv")
    out: dict[date, Decimal] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get("IUDSOIA") or "").strip()
            if raw:
                out[_boe_date(row["DATE"])] = Decimal(raw)
    return out


def cash_sleeve_bars() -> tuple[list[Bar], date]:
    """SONIA index until XSTR, then the traded XSTR price with its pay dates.

    The SONIA stretch is scaled to XSTR's first close. XSTR distributions stay
    on the bar so the simulator can accrue them. They are not baked into the close.
    """
    calendar = xlon()
    if not (YAHOO / "XSTR.csv").is_file():
        raise ResearchError("cash benchmark needs .research-cache/yahoo/XSTR.csv")
    raw = read_ohlcv_csv(YAHOO / "XSTR.csv")
    pays = xstr_pay_dates()
    market: dict[date, Bar] = {}
    for bar in raw:
        if not calendar.is_trading_day(bar.session):
            continue
        scale = Decimal(100) if bar.quoted_in in {"GBp", "GBX"} else Decimal(1)
        dividend = None if bar.dividend in (None, ZERO) else bar.dividend / scale
        market[bar.session] = Bar(
            session=bar.session,
            open=bar.open / scale,
            high=bar.high / scale,
            low=bar.low / scale,
            close=bar.close / scale,
            volume=bar.volume,
            dividend=dividend,
            quoted_in="GBP",
            pay_date=pays.get(bar.session) if dividend else None,
        )
    if not market:
        raise ResearchError("XSTR cache has no XLON sessions")
    first = min(market)
    last = max(market)
    sonia = _sonia()
    sessions = calendar.trading_days(date(1999, 1, 4), last)
    carried: list[tuple[date, Decimal]] = []
    last_rate: Decimal | None = None
    for session in sessions:
        if session in sonia:
            last_rate = sonia[session]
        if last_rate is not None:
            carried.append((session, last_rate))
    index = dict(compound_annual_percent(carried))
    if first not in index or index[first] <= 0 or first not in market:
        raise ResearchError("SONIA index does not reach the XSTR splice")
    scale_to_xstr = market[first].close / index[first]
    bars: list[Bar] = []
    for session in sessions:
        if session < first:
            if session not in index:
                continue
            level = index[session] * scale_to_xstr
            bars.append(
                Bar(
                    session=session,
                    open=level,
                    high=level,
                    low=level,
                    close=level,
                    quoted_in="GBP",
                )
            )
            continue
        if session in market:
            bars.append(market[session])
            continue
        previous = bars[-1]
        bars.append(
            Bar(
                session=session,
                open=previous.close,
                high=previous.close,
                low=previous.close,
                close=previous.close,
                quoted_in="GBP",
            )
        )
    indexed = dict(distribution_total_return([(bar.session, bar.close, bar.dividend or ZERO) for bar in bars]))
    bars = [replace(bar, signal_level=indexed[bar.session]) for bar in bars]
    return bars, first


def write_cash_benchmark(path: Path | None = None) -> Path:
    """Write the accrual total-return index. Creates the gitignored export directory."""
    bars, xstr_start = cash_sleeve_bars()
    indexed = dict(distribution_total_return([(bar.session, bar.close, bar.dividend or ZERO) for bar in bars]))
    destination = path or EXPORT
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "date",
                "traded_close_gbp",
                "dividend_gbp",
                "pay_date",
                "total_return_index",
                "source",
            ]
        )
        for bar in bars:
            source = "sonia_compounded" if bar.session < xstr_start else "xstr_traded_price"
            writer.writerow(
                [
                    bar.session.isoformat(),
                    format(bar.close, "f"),
                    format(bar.dividend or ZERO, "f"),
                    "" if bar.pay_date is None else bar.pay_date.isoformat(),
                    format(indexed[bar.session], "f"),
                    source,
                ]
            )
    return destination

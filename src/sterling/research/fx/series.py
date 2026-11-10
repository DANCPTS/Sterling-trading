"""Local FX caches. This module does not open a network connection.

Bank of England spots are foreign-currency units per one pound. A print dated
``t`` becomes usable on the next panel date for signals, and the loader records
daily interest rates as usable on the next London business day. BIS policy
rates are monthly: the month-M observation becomes usable on the 15th of month
M+2. Rates are stored as annual decimals (0.01 means 1% per year).
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sterling.research.calendar import WeekdayCalendar, xlon
from sterling.research.errors import DataValidationError, ResearchError
from sterling.research.fx.config import CurrencySpec, FxResearchConfig

PERCENT = Decimal(100)
MIN_PERCENT = Decimal("-2")
MAX_PERCENT = Decimal("30")


@dataclass(frozen=True, slots=True)
class StepSeries:
    """Observations ordered by the date they may first be used."""

    available: tuple[date, ...]
    observed: tuple[date, ...]
    values: tuple[Decimal, ...]
    stale_days: int

    def as_of(self, day: date) -> tuple[date, Decimal] | None:
        index = bisect_right(self.available, day) - 1
        if index < 0:
            return None
        observed = self.observed[index]
        if (day - observed).days > self.stale_days:
            return None
        return observed, self.values[index]


@dataclass(frozen=True, slots=True)
class Market:
    codes: tuple[str, ...]
    spots: tuple[tuple[str, tuple[tuple[date, Decimal], ...]], ...]
    panel: tuple[date, ...]
    sonia: StepSeries
    bank_rate: StepSeries
    policy: tuple[tuple[str, StepSeries], ...]
    currencies: tuple[CurrencySpec, ...]

    def spot_map(self, code: str) -> dict[date, Decimal]:
        for name, series in self.spots:
            if name == code:
                return dict(series)
        raise ResearchError(f"no spot series for {code}")

    def policy_series(self, code: str) -> StepSeries:
        for name, series in self.policy:
            if name == code:
                return series
        raise ResearchError(f"no policy series for {code}")


def month_end_dates(panel: tuple[date, ...] | list[date]) -> tuple[date, ...]:
    last: dict[tuple[int, int], date] = {}
    for day in panel:
        last[(day.year, day.month)] = day
    return tuple(last[key] for key in sorted(last))


def monthly_available_from(month_end: date) -> date:
    month = month_end.month + 2
    year = month_end.year
    if month > 12:
        month -= 12
        year += 1
    return date(year, month, 15)


def end_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def percent_to_decimal(text: str, *, label: str) -> Decimal:
    try:
        percent = Decimal(text)
    except InvalidOperation as exc:
        raise DataValidationError(f"{label} is not a decimal percent") from exc
    if not percent.is_finite() or percent < MIN_PERCENT or percent > MAX_PERCENT:
        raise DataValidationError(f"{label} percent {percent} is outside {-2} to 30")
    return percent / PERCENT


def build_step_series(
    points: list[tuple[date, date, Decimal]],
    *,
    stale_days: int,
) -> StepSeries:
    """``points`` are ``(available_from, observed, annual_decimal)``."""
    if stale_days < 1:
        raise ResearchError("stale-rate window must be at least one day")
    ordered = sorted(points, key=lambda item: (item[0], item[1]))
    collapsed: list[tuple[date, date, Decimal]] = []
    for available, observed, value in ordered:
        if collapsed and collapsed[-1][0] == available:
            if observed >= collapsed[-1][1]:
                collapsed[-1] = (available, observed, value)
            continue
        collapsed.append((available, observed, value))
    return StepSeries(
        available=tuple(item[0] for item in collapsed),
        observed=tuple(item[1] for item in collapsed),
        values=tuple(item[2] for item in collapsed),
        stale_days=stale_days,
    )


def _boe_date(text: str) -> date:
    try:
        return datetime.strptime(text.strip(), "%d %b %Y").date()
    except ValueError as exc:
        raise DataValidationError(f"BoE date {text!r} is not 'DD Mon YYYY'") from exc


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise DataValidationError(f"missing FX cache file {path.name}; the simulator does not download")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DataValidationError(f"{path.name} has no header")
        return [{key: (value or "").strip() for key, value in row.items() if key is not None} for row in reader]


def load_market(cache: Path, config: FxResearchConfig) -> Market:
    spot_rows = _read_rows(cache / "boe_spot_rates.csv")
    rate_rows = _read_rows(cache / "boe_rates.csv")
    policy_rows = _read_rows(cache / "bis_cbpol.csv")
    codes = tuple(item.code for item in config.currencies)
    boe_codes = {item.code: item.boe_series for item in config.currencies}
    columns: dict[str, dict[date, Decimal]] = {code: {} for code in codes}
    for row in spot_rows:
        if "DATE" not in row:
            raise DataValidationError("BoE spot file needs a DATE column")
        if not row["DATE"]:
            continue
        day = _boe_date(row["DATE"])
        for code, series_code in boe_codes.items():
            raw = row.get(series_code, "")
            if not raw:
                continue
            try:
                spot = Decimal(raw)
            except InvalidOperation as exc:
                raise DataValidationError(f"{code} spot on {day.isoformat()} is not a decimal") from exc
            if spot <= 0:
                raise DataValidationError(f"{code} spot on {day.isoformat()} is not positive")
            if day in columns[code]:
                raise DataValidationError(f"duplicate {code} spot on {day.isoformat()}")
            columns[code][day] = spot
    shared = set(columns[codes[0]])
    for code in codes[1:]:
        shared &= set(columns[code])
    panel = tuple(sorted(shared))
    if len(panel) < config.min_month_ends + 2:
        raise DataValidationError("spot panel is too short for the pre-registered lookback")
    spots = tuple((code, tuple((day, columns[code][day]) for day in panel)) for code in codes)
    calendar = xlon()
    sonia = _daily_rate(rate_rows, "IUDSOIA", "SONIA", calendar, config.stale_rate_days)
    bank = _daily_rate(rate_rows, "IUDBEDR", "Bank Rate", calendar, config.stale_rate_days)
    areas = {item.bis_area: item.code for item in config.currencies}
    buckets: dict[str, list[tuple[date, date, Decimal]]] = {code: [] for code in codes}
    for row in policy_rows:
        area = row.get("REF_AREA", "")
        matched = areas.get(area)
        if matched is None:
            continue
        period = row.get("TIME_PERIOD", "")
        raw = row.get("OBS_VALUE", "")
        if not period or not raw:
            continue
        try:
            year_text, month_text = period.split("-")
            observed = end_of_month(int(year_text), int(month_text))
        except (ValueError, IndexError) as exc:
            raise DataValidationError(f"BIS period {period!r} is not YYYY-MM") from exc
        value = percent_to_decimal(raw, label=f"{matched} {period}")
        buckets[matched].append((monthly_available_from(observed), observed, value))
    policy: list[tuple[str, StepSeries]] = []
    for code in codes:
        if not buckets[code]:
            raise DataValidationError(f"BIS policy file has no rows for {code}")
        policy.append((code, build_step_series(buckets[code], stale_days=config.stale_rate_days)))
    return Market(
        codes=codes,
        spots=spots,
        panel=panel,
        sonia=sonia,
        bank_rate=bank,
        policy=tuple(policy),
        currencies=config.currencies,
    )


def _daily_rate(
    rows: list[dict[str, str]],
    column: str,
    label: str,
    calendar: WeekdayCalendar,
    stale_days: int,
) -> StepSeries:
    points: list[tuple[date, date, Decimal]] = []
    seen: set[date] = set()
    for row in rows:
        raw = row.get(column, "")
        if not raw or not row.get("DATE"):
            continue
        observed = _boe_date(row["DATE"])
        if observed in seen:
            raise DataValidationError(f"duplicate {label} print on {observed.isoformat()}")
        seen.add(observed)
        available = calendar.next_trading_day(observed)
        points.append((available, observed, percent_to_decimal(raw, label=f"{label} {observed.isoformat()}")))
    if not points:
        raise DataValidationError(f"{label} cache is empty")
    return build_step_series(points, stale_days=stale_days)

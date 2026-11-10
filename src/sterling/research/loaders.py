"""Local CSV loaders.

Nothing in this module opens a socket. Yahoo and Stooq files are read from a
directory the caller already has, ideally a gitignored cache.

Yahoo's chart ``period2`` is an exclusive UTC timestamp. A request with
``period2`` at 2026-09-22T00:00Z returns a last daily bar of 2026-09-21, and
``period2`` at 2026-09-23T00:00Z returns 2026-09-22. A draft check whose ``.L``
histories all ended on 2026-09-21 matches that window. It is not evidence that
the provider stopped. Chart requests made on 2026-11-09 returned bars through
2026-11-09. Do not commit downloaded files.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from sterling.research.bars import Bar
from sterling.research.errors import DataValidationError, NetworkDisabled, UnverifiedInput
from sterling.research.instruments import CandidateUniverse
from sterling.research.money import ZERO, parse_decimal


def assert_local_path(path: Path) -> None:
    text = str(path)
    # pathlib collapses "https://" to "https:/", so match the scheme rather than the raw URL.
    if text.startswith(("http:", "https:", "ftp:")) or "://" in text:
        raise NetworkDisabled("Research loaders do not fetch URLs. Place a CSV in a local gitignored cache.")


def research_cache_dir(root: Path | None = None) -> Path:
    """Return the gitignored cache directory, creating it if needed."""
    directory = root if root is not None else Path(".research-cache")
    assert_local_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _cell(row: dict[str, str], *names: str) -> str | None:
    for name in names:
        if name in row and row[name].strip():
            return row[name].strip()
    return None


def _decimal(text: str, label: str) -> Decimal:
    return parse_decimal(text, label=label)


def _parse_date(text: str) -> date:
    cleaned = text.strip()
    if len(cleaned) == 8 and cleaned.isdigit():
        return date(int(cleaned[0:4]), int(cleaned[4:6]), int(cleaned[6:8]))
    return date.fromisoformat(cleaned[:10])


def bars_from_rows(
    rows: list[dict[str, str]],
    *,
    default_quoted_in: str | None,
    label: str,
) -> tuple[Bar, ...]:
    bars: list[Bar] = []
    for index, row in enumerate(rows, start=1):
        raw_date = _cell(row, "date", "Date", "datetime")
        if raw_date is None:
            raise DataValidationError(f"{label} row {index} has no date")
        quoted = _cell(row, "quoted_in", "currency", "quote_currency") or default_quoted_in
        if quoted is None:
            raise UnverifiedInput(f"{label} row {index} has no quote unit and none was supplied")

        def number(name: str, *aliases: str, source: dict[str, str] = row, row_number: int = index) -> Decimal:
            text = _cell(source, name, *aliases)
            if text is None:
                raise DataValidationError(f"{label} row {row_number} is missing {name}")
            return _decimal(text, f"{label} {name}")

        adjusted_text = _cell(row, "adjusted_close", "adj_close", "Adj Close")
        dividend_text = _cell(row, "dividend", "dividends")
        pay_text = _cell(row, "pay_date", "payment_date", "paydate")
        volume_text = _cell(row, "volume", "Volume")
        bars.append(
            Bar(
                session=_parse_date(raw_date),
                open=number("open", "Open"),
                high=number("high", "High"),
                low=number("low", "Low"),
                close=number("close", "Close"),
                volume=_decimal(volume_text, "volume") if volume_text is not None else ZERO,
                adjusted_close=_decimal(adjusted_text, "adjusted_close") if adjusted_text is not None else None,
                dividend=_decimal(dividend_text, "dividend") if dividend_text is not None else None,
                quoted_in=quoted,
                pay_date=_parse_date(pay_text) if pay_text is not None else None,
            )
        )
    return tuple(bars)


def read_ohlcv_csv(path: Path, *, default_quoted_in: str | None = None) -> tuple[Bar, ...]:
    assert_local_path(path)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DataValidationError(f"{path.name} has no header")
        return bars_from_rows(list(reader), default_quoted_in=default_quoted_in, label=path.name)


def read_stooq_csv(path: Path, *, quoted_in: str | None) -> tuple[Bar, ...]:
    """Parse a local Stooq daily file. The price unit is not assumed.

    Stooq history for this universe is UNVERIFIED, and the Yahoo pence/pounds
    label is not applied to a Stooq file. Pass ``quoted_in`` explicitly.
    """
    if quoted_in is None or quoted_in.strip().upper() == "UNVERIFIED":
        raise UnverifiedInput(
            "Stooq price unit is UNVERIFIED. Pass quoted_in explicitly (GBP, GBp, or GBX). "
            "The loader will not guess from a Yahoo currency code."
        )
    return read_ohlcv_csv(path, default_quoted_in=quoted_in)


class LocalCsvSource:
    """Read ``{ticker}.csv`` files from one directory. No network."""

    def __init__(self, directory: Path, universe: CandidateUniverse | None = None) -> None:
        assert_local_path(directory)
        if not directory.is_dir():
            raise DataValidationError(f"{directory} is not a directory")
        self.directory = directory
        self.universe = universe

    def load(self, ticker: str, *, source: str = "local") -> tuple[Bar, ...]:
        path = self.directory / f"{ticker}.csv"
        if source == "stooq":
            raise UnverifiedInput("Use read_stooq_csv with an explicit quote unit. Stooq history is UNVERIFIED.")
        default_unit: str | None = None
        if self.universe is not None:
            default_unit = self.universe.by_ticker(ticker).yahoo_quote_currency
        bars = read_ohlcv_csv(path, default_quoted_in=default_unit)
        if default_unit is not None:
            for bar in bars:
                file_unit = bar.quoted_in
                if _conflicts(file_unit, default_unit):
                    raise DataValidationError(
                        f"{ticker} file quote unit {file_unit} conflicts with the draft universe unit {default_unit}"
                    )
        return bars


def _conflicts(left: str, right: str) -> bool:
    from sterling.research.bars import quote_scale

    return quote_scale(left) != quote_scale(right)

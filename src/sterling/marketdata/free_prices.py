"""Free price readers. Tests pass canned bytes. Nothing here is committed market data.

Yahoo chart responses are not redistributable. Keep them out of the public repo.
The daily bar timestamp from Yahoo is the London session open. That timestamp
is the open's observation time. It is not the time the close was known.
Stooq is parsed only for tests. It is not the live second source: the daily
CSV URL does not return a CSV from this environment.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
ISHARES_PORTFOLIOS = {
    "CUKX": "253716",
    "IGLT": "251806",
    "AGBP": "291771",
    "SGLN": "258441",
}
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

YAHOO_SYMBOLS = {
    "VWRP": "VWRP.L",
    "CUKX": "CUKX.L",
    "IGLT": "IGLT.L",
    "AGBP": "AGBP.L",
    "SGLN": "SGLN.L",
    "XSTR": "XSTR.L",
}
STOOQ_SYMBOLS = {
    "VWRP": "vwrp.uk",
    "CUKX": "cukx.uk",
    "IGLT": "iglt.uk",
    "AGBP": "agbp.uk",
    "SGLN": "sgln.uk",
    "XSTR": "xstr.uk",
}


@dataclass(frozen=True, slots=True)
class RawPrint:
    ticker: str
    session: date
    price: Decimal
    quoted_in: str
    published_at: datetime
    source: str
    open_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class YahooBar:
    """One daily bar. ``open_at`` is the chart timestamp. ``close_at`` is 16:30 London."""

    ticker: str
    session: date
    open_price: Decimal | None
    close_price: Decimal | None
    quoted_in: str
    open_at: datetime
    close_at: datetime


@dataclass(frozen=True, slots=True)
class NavPrint:
    """Issuer NAV. ``valuation_date`` is not the publication time."""

    ticker: str
    valuation_date: date
    price: Decimal
    quoted_in: str
    source: str


def next_london_morning(valuation: date) -> datetime:
    """Unverified assumption only. Do not use this as the time a NAV became available."""
    day = valuation + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, 8, 0, tzinfo=LONDON).astimezone(UTC)


def london_session(moment: datetime, hour: int, minute: int) -> datetime:
    """Clock time in London on the same London date, as UTC."""
    local = moment.astimezone(LONDON)
    stamped = datetime(local.year, local.month, local.day, hour, minute, tzinfo=LONDON)
    return stamped.astimezone(UTC)


def yahoo_bars(ticker: str, payload: bytes) -> tuple[YahooBar, ...]:
    """Read Yahoo daily opens and closes. A bar with no timestamp is dropped."""
    loaded = json.loads(payload.decode("utf-8"))
    result = loaded["chart"]["result"][0]
    meta = result.get("meta") or {}
    quoted = str(meta.get("currency") or "GBP")
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    opens = quote.get("open") or []
    closes = quote.get("close") or []
    rows: list[YahooBar] = []
    for index, stamp in enumerate(timestamps):
        if stamp is None:
            continue
        open_at = datetime.fromtimestamp(int(stamp), tz=UTC)
        session = open_at.astimezone(LONDON).date()
        open_price = _positive(opens[index] if index < len(opens) else None)
        close_price = _positive(closes[index] if index < len(closes) else None)
        if open_price is None and close_price is None:
            continue
        rows.append(YahooBar(ticker, session, open_price, close_price, quoted, open_at, london_session(open_at, 16, 30)))
    return tuple(rows)


def yahoo_chart(ticker: str, payload: bytes) -> tuple[RawPrint, ...]:
    loaded = json.loads(payload.decode("utf-8"))
    result = loaded["chart"]["result"][0]
    meta = result.get("meta") or {}
    quoted = str(meta.get("currency") or "GBP")
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    rows: list[RawPrint] = []
    for stamp, close in zip(timestamps, closes, strict=False):
        if stamp is None or close is None:
            continue
        try:
            price = Decimal(str(close))
        except InvalidOperation:
            continue
        if price <= 0:
            continue
        published = datetime.fromtimestamp(int(stamp), tz=UTC)
        rows.append(RawPrint(ticker, published.date(), price, quoted, published, "yahoo-chart"))
    return tuple(rows)


def stooq_csv(ticker: str, payload: bytes, *, quoted_in: str) -> tuple[RawPrint, ...]:
    text = payload.decode("utf-8")
    rows: list[RawPrint] = []
    for record in csv.DictReader(StringIO(text)):
        raw_date = record.get("Date") or record.get("date")
        raw_close = record.get("Close") or record.get("close")
        if not raw_date or not raw_close or raw_close in {"", "null"}:
            continue
        try:
            price = Decimal(raw_close)
            session = date.fromisoformat(raw_date[:10])
        except (InvalidOperation, ValueError):
            continue
        if price <= 0:
            continue
        published = datetime(session.year, session.month, session.day, 16, 30, tzinfo=UTC)
        rows.append(RawPrint(ticker, session, price, quoted_in, published, "stooq"))
    return tuple(rows)


def first_seen_many(path: Path, identities: list[str], now: datetime) -> dict[str, datetime]:
    """Persist the first UTC time each value was seen. Later calls return the original time."""
    stamped = now.astimezone(UTC)
    store: dict[str, str] = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            store = {str(key): str(value) for key, value in loaded.items()}
    changed = False
    found: dict[str, datetime] = {}
    for identity in identities:
        if identity not in store:
            store[identity] = stamped.isoformat()
            changed = True
        found[identity] = datetime.fromisoformat(store[identity])
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(store, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    return found


def vanguard_nav_html(ticker: str, payload: bytes) -> tuple[NavPrint, ...]:
    """Latest NAV embedded in the Vanguard UK professional fund page."""
    text = payload.decode("utf-8", errors="replace")
    match = re.search(
        r'"navPrices":\{"items":\[\{"asOfDate":"(\d{4}-\d{2}-\d{2})","currencyCode":"([A-Z]{3})","price":([0-9]+(?:\.[0-9]+)?)',
        text,
    )
    if not match:
        return ()
    return (NavPrint(ticker, date.fromisoformat(match.group(1)), Decimal(match.group(3)), match.group(2), "vanguard-nav"),)


def justetf_quotes(ticker: str, payload: bytes, *, quoted_in: str) -> tuple[NavPrint, ...]:
    """Parse a JustETF quote response.

    ``quoted_in`` is the currency the caller requested. It is not the venue's
    trading currency. Xetra lists XSTR as DXS1 in EUR, so a GBP response is a
    converted display and is not a second source.
    """
    loaded = json.loads(payload.decode("utf-8"))
    venue = str(loaded.get("quoteTradingVenue") or "unknown").lower()
    rows: list[NavPrint] = []
    for price_key, date_key in (("latestQuote", "latestQuoteDate"), ("previousQuote", "previousQuoteDate")):
        raw = loaded.get(price_key) or {}
        session_text = loaded.get(date_key)
        price = _positive(raw.get("raw") if isinstance(raw, dict) else None)
        if price is None or not isinstance(session_text, str):
            continue
        rows.append(NavPrint(ticker, date.fromisoformat(session_text[:10]), price, quoted_in, f"justetf-{venue}"))
    return tuple(rows)


def usd_on_valuation_date(price: Decimal, valuation: date, fx: dict[date, Decimal]) -> Decimal | None:
    """Convert with the Bank of England rate dated that valuation date, not a later day."""
    rate = fx.get(valuation)
    if rate is None or rate <= 0:
        return None
    return (price / rate).quantize(Decimal("0.000001"))


def ishares_nav_xls(ticker: str, payload: bytes) -> tuple[NavPrint, ...]:
    """Read the Historical NAVs sheet of an iShares XML spreadsheet.

    The date in the sheet is the valuation date. This function does not invent
    a publication time.
    """
    text = payload.decode("utf-8", errors="replace")
    sheet = _worksheet(text, "Historical NAVs")
    if not sheet:
        return ()
    quoted = _labelled_currency(text)
    rows: list[NavPrint] = []
    for cells in _xml_rows(sheet):
        if not cells:
            continue
        session = _ishares_date(cells[0])
        price = _positive(cells[1]) if len(cells) > 1 else None
        if session is None or price is None or quoted is None:
            continue
        rows.append(NavPrint(ticker, session, price, quoted, "ishares-nav"))
    return tuple(rows)


def boe_usd_per_gbp(payload: bytes) -> tuple[tuple[date, Decimal], ...]:
    """Bank of England XUDLUSS: US dollars per one pound. The date is the observation date."""
    series = boe_columns(payload)
    rows = series.get("XUDLUSS")
    if rows is None and len(series) == 1:
        rows = next(iter(series.values()))
    if not rows:
        return ()
    return tuple(sorted(rows.items()))


def boe_columns(payload: bytes) -> dict[str, dict[date, Decimal]]:
    """Read a Bank of England CSV. Each series is foreign currency per one pound."""
    text = payload.decode("utf-8", errors="replace")
    header: list[str] = []
    columns: dict[str, dict[date, Decimal]] = {}
    for line in text.splitlines():
        parts = [item.strip() for item in line.split(",")]
        if not parts or not parts[0]:
            continue
        if parts[0].upper() == "DATE":
            header = parts[1:]
            continue
        observed = _boe_date(parts[0])
        if observed is None or not header:
            continue
        for name, raw in zip(header, parts[1:], strict=False):
            price = _positive(raw)
            if price is None:
                continue
            columns.setdefault(name, {})[observed] = price
    return columns


def collect_free(getter: Callable[[str], bytes]) -> tuple[tuple[RawPrint, ...], tuple[RawPrint, ...]]:
    """Read Yahoo and Stooq through the injected getter. Stooq currency stays unverified."""
    yahoo: list[RawPrint] = []
    stooq: list[RawPrint] = []
    for ticker, symbol in YAHOO_SYMBOLS.items():
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2y"
        yahoo.extend(yahoo_chart(ticker, fetch_bytes(url, getter)))
    for ticker, symbol in STOOQ_SYMBOLS.items():
        url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
        stooq.extend(stooq_csv(ticker, fetch_bytes(url, getter), quoted_in="UNVERIFIED"))
    return tuple(yahoo), tuple(stooq)


def _positive(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        return None
    if price <= 0:
        return None
    return price


def _worksheet(text: str, name: str) -> str:
    match = re.search(rf'<ss:Worksheet[^>]*ss:Name="{re.escape(name)}".*?</ss:Worksheet>', text, re.DOTALL)
    return match.group(0) if match else ""


def _xml_rows(sheet: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in re.findall(r"<ss:Row\b.*?</ss:Row>", sheet, re.DOTALL):
        rows.append(re.findall(r"<ss:Data\b[^>]*>([^<]*)</ss:Data>", row))
    return rows


def _labelled_currency(text: str) -> str | None:
    for label in ("Share Class Currency", "Base Currency", "Currency"):
        match = re.search(
            rf">{re.escape(label)}</ss:Data>.*?<ss:Data\b[^>]*>\s*([A-Z]{{3}})\s*</ss:Data>",
            text,
            re.DOTALL,
        )
        if match:
            return match.group(1)
    return None


def _ishares_date(value: str) -> date | None:
    match = re.fullmatch(r"(\d{1,2})/([A-Za-z]+)/(\d{4})", value.strip())
    if not match:
        return None
    month = _MONTHS.get(match.group(2).lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _boe_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%d %b %Y").date()
    except ValueError:
        return None


def fetch_bytes(url: str, getter: Callable[[str], bytes]) -> bytes:
    """Read one URL through the injected getter. Tests do not pass a live getter."""
    if not url.startswith("https://"):
        raise ValueError("free price URLs must be https")
    return getter(url)

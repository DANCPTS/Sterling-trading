"""Free price readers. Tests pass canned bytes. Nothing here is committed market data.

Yahoo chart responses are not redistributable. Keep them in a gitignored cache.
Stooq units are unverified until the caller names the quote currency. A missing
timestamp is dropped rather than stamped with the fetch time.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO

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


def fetch_bytes(url: str, getter: Callable[[str], bytes]) -> bytes:
    """Read one URL through the injected getter. Tests do not pass a live getter."""
    if not url.startswith("https://"):
        raise ValueError("free price URLs must be https")
    return getter(url)

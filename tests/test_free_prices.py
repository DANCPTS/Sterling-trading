"""Canned Yahoo and Stooq bytes. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sterling.marketdata.free_prices import collect_free, stooq_csv, yahoo_chart

STAMP = 1717774200  # 2024-06-07 15:30:00 UTC


def _yahoo(price: str, currency: str) -> bytes:
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": currency},
                    "timestamp": [STAMP, None],
                    "indicators": {"quote": [{"close": [price, None]}]},
                }
            ]
        }
    }
    return json.dumps(payload).encode("utf-8")


def test_yahoo_keeps_currency_and_drops_a_missing_timestamp() -> None:
    rows = yahoo_chart("CUKX", _yahoo("10150.5", "GBp"))
    assert len(rows) == 1
    assert rows[0].quoted_in == "GBp"
    assert rows[0].published_at == datetime.fromtimestamp(STAMP, tz=UTC)
    assert rows[0].source == "yahoo-chart"


def test_stooq_currency_is_the_caller_label() -> None:
    payload = b"Date,Close\n2024-06-07,101.50\n2024-06-08,null\n"
    rows = stooq_csv("VWRP", payload, quoted_in="GBP")
    assert len(rows) == 1
    assert rows[0].quoted_in == "GBP"
    assert rows[0].published_at.hour == 16


def test_collect_free_uses_only_the_injected_getter() -> None:
    seen: list[str] = []

    def _getter(url: str) -> bytes:
        seen.append(url)
        if "finance.yahoo.com" in url:
            return _yahoo("100", "GBP")
        return b"Date,Close\n2024-06-07,100\n"

    yahoo, stooq = collect_free(_getter)
    assert len(yahoo) == 6
    assert len(stooq) == 6
    assert all(item.quoted_in == "UNVERIFIED" for item in stooq)
    assert all(item.startswith("https://") for item in seen)
    assert len(seen) == 12

"""Canned Yahoo and Stooq bytes. No network."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from sterling.marketdata.free_prices import boe_usd_per_gbp, collect_free, ishares_nav_xls, stooq_csv, yahoo_bars, yahoo_chart

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


def test_yahoo_open_is_not_given_the_close_time() -> None:
    # 09 Nov 2026 08:00 UTC is the London open in winter. The close is 16:30 London.
    opened = int(datetime(2026, 11, 9, 8, 0, tzinfo=UTC).timestamp())
    payload = json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "GBP"},
                        "timestamp": [opened],
                        "indicators": {"quote": [{"open": [139.5], "close": [140.0]}]},
                    }
                ]
            }
        }
    ).encode()
    bar = yahoo_bars("VWRP", payload)[0]
    assert bar.open_at == datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    assert bar.close_at > bar.open_at
    assert bar.open_price == Decimal("139.5")
    assert bar.close_price == Decimal("140.0")


def test_ishares_nav_keeps_the_valuation_date_and_currency() -> None:
    payload = b"""<?xml version="1.0"?>
<ss:Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
<ss:Worksheet ss:Name="Fund Header"><ss:Table><ss:Row>
<ss:Cell><ss:Data ss:Type="String">Share Class Currency</ss:Data></ss:Cell>
<ss:Cell><ss:Data ss:Type="String">GBP</ss:Data></ss:Cell>
</ss:Row></ss:Table></ss:Worksheet>
<ss:Worksheet ss:Name="Historical NAVs"><ss:Table>
<ss:Row><ss:Cell><ss:Data ss:Type="String">Historical NAVs</ss:Data></ss:Cell></ss:Row>
<ss:Row><ss:Cell><ss:Data ss:Type="String">09/Nov/2026</ss:Data></ss:Cell>
<ss:Cell><ss:Data ss:Type="Number">221.86</ss:Data></ss:Cell></ss:Row>
</ss:Table></ss:Worksheet>
</ss:Workbook>"""
    rows = ishares_nav_xls("CUKX", payload)
    assert rows[0].valuation_date.isoformat() == "2026-11-09"
    assert rows[0].quoted_in == "GBP"
    assert rows[0].price == Decimal("221.86")
    assert not hasattr(rows[0], "published_at")


def test_boe_spot_is_dollars_per_pound() -> None:
    rows = boe_usd_per_gbp(b"DATE,XUDLUSS\n09 Nov 2026,1.3403\n")
    assert rows[0] == (date(2026, 11, 9), Decimal("1.3403"))


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

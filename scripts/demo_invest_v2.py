"""Two SYNTHETIC/MOCK paper-v2 weeks. No xAI call and no live prices."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.invest.book import Book
from sterling.research.invest.daily import load_book, run_daily
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.report import write_dashboard
from sterling.research.invest.schema import CASH, FUNDS, allocation_json

LABEL = "SYNTHETIC"


def main() -> None:
    os.environ.setdefault("STERLING_SYSTEM", "sterling-invest")
    os.environ.setdefault("STERLING_EXPERIMENT", "paper-v2")
    os.environ.setdefault("STERLING_EVIDENCE_LANE", "mock")
    start = datetime(2026, 11, 2, 16, 30, tzinfo=UTC)
    sessions = [start.date() + timedelta(days=offset) for offset in range(12)]
    sessions = [day for day in sessions if day.weekday() < 5]
    sonia = ((start.date() - timedelta(days=3), Decimal("4.00")),)
    history = {name: tuple((day, Decimal("100") + Decimal(index)) for index, day in enumerate(sessions)) for name in FUNDS}
    path = Path(os.environ.get("STERLING_DEMO_LOG", "/tmp/sterling-invest-v2.jsonl"))
    if path.exists():
        path.unlink()
    print("DATA LABEL: SYNTHETIC/MOCK. Not a market feed and not an xAI call.")
    _decide(path, Book(), sessions[0], {name: Decimal("100") for name in FUNDS}, _equal(), start)
    for index, session in enumerate(sessions[1:], start=1):
        if session == sessions[5]:
            book = load_book(path)
            prices = {name: Decimal("100") + Decimal(index) for name in FUNDS}
            _decide(path, book, session, prices, _second(), start + timedelta(days=7))
        opens = {name: Decimal("100") + Decimal(index) for name in FUNDS}
        result = run_daily(
            log_path=path,
            session=session,
            opens=opens,
            close_history=history,
            sonia=sonia,
            recorded_at=datetime(session.year, session.month, session.day, 17, 0, tzinfo=UTC),
            open_source="SYNTHETIC-open",
            data_label=LABEL,
        )
        print(f"day {session.isoformat()} status={result['status']} nav={result['nav_gbp']} calls={result['provider_calls']}")
    page = Path(os.environ.get("STERLING_DASHBOARD", "/tmp/sterling-dashboard.html"))
    if write_dashboard(path, page) != 0:
        raise SystemExit("dashboard was not written because verification failed")
    print(f"dashboard {page}")


def _decide(path: Path, book: Book, session: object, prices: dict[str, Decimal], raw: str, when: datetime) -> None:
    result = run_invest_week(
        log_path=path,
        book=book,
        prices=prices,
        tradable=set(prices),
        as_of=when,
        provider=MockGrokProvider(steps=(ScriptStep(raw_response=raw),), response_format="allocation"),
        prompt="synthetic paper-v2",
        mode="simulated_clock",
    )
    print(f"decision {session} calls={result['provider_calls']} gate={result['gate']}")


def _equal() -> str:
    weight = format(Decimal(1) / Decimal(7), "f")
    return allocation_json({name: weight for name in (*FUNDS, CASH)})


def _second() -> str:
    weights = {name: "0" for name in FUNDS}
    weights["VWRP"] = "0.50"
    weights["IGLT"] = "0.30"
    weights[CASH] = "0.20"
    return allocation_json(weights)


if __name__ == "__main__":
    main()

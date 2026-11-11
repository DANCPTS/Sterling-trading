"""Two offline paper-v2 weeks and a benchmark table. No xAI call."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.invest.benchmarks import benchmark_table, summarise_arm
from sterling.research.invest.book import Book, mark, settle_pending
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.schema import CASH, FUNDS, allocation_json


def main() -> None:
    os.environ.setdefault("STERLING_SYSTEM", "sterling-invest")
    os.environ.setdefault("STERLING_EXPERIMENT", "paper-v2")
    os.environ.setdefault("STERLING_EVIDENCE_LANE", "mock")
    start = datetime(2026, 11, 2, 16, 30, tzinfo=UTC)
    sessions = []
    for offset in range(12):
        day = start.date() + timedelta(days=offset)
        if day.weekday() < 5:
            sessions.append(day)
    closes = {name: tuple((day, Decimal("100") + Decimal(index)) for index, day in enumerate(sessions)) for name in FUNDS}
    book = Book()
    prices = {name: Decimal("100") for name in FUNDS}
    log = os.environ.get("STERLING_DEMO_LOG", "/tmp/sterling-invest-v2.jsonl")
    from pathlib import Path

    path = Path(log)
    if path.exists():
        path.unlink()
    first = run_invest_week(
        log_path=path,
        book=book,
        prices=prices,
        tradable=set(FUNDS),
        as_of=start,
        provider=MockGrokProvider(seed=1, response_format="allocation"),
        prompt="paper-v2 week 1",
        mode="simulated_clock",
    )
    print(f"week1 status={first['status']} calls={first['provider_calls']} gate={first['gate']} nav={first['nav_gbp']}")
    for index, session in enumerate(sessions[1:6], start=1):
        day_prices = {name: Decimal("100") + index for name in FUNDS}
        fills = settle_pending(book, day_prices, set(FUNDS), session)
        marked = mark(book, day_prices, session)
        nav = marked[0] if marked else book.cash
        print(
            f"day {session.isoformat()} fills={len(fills)} nav={nav} "
            f"cash={book.cash} trades={book.trades} costs={book.cumulative_costs}"
        )
    second_weights = {name: "0" for name in FUNDS}
    second_weights["VWRP"] = "0.50"
    second_weights["IGLT"] = "0.30"
    second_weights[CASH] = "0.20"
    second = run_invest_week(
        log_path=path,
        book=book,
        prices={name: Decimal("106") for name in FUNDS},
        tradable=set(FUNDS),
        as_of=start + timedelta(days=7),
        provider=MockGrokProvider(steps=(ScriptStep(raw_response=allocation_json(second_weights)),), response_format="allocation"),
        prompt="paper-v2 week 2",
        mode="simulated_clock",
    )
    fills = settle_pending(book, {name: Decimal("106") for name in FUNDS}, set(FUNDS), (start + timedelta(days=7)).date())
    marked = mark(book, {name: Decimal("106") for name in FUNDS}, (start + timedelta(days=7)).date())
    nav = marked[0] if marked else book.cash
    print(f"week2 status={second['status']} calls={second['provider_calls']} gate={second['gate']} fills={len(fills)} nav={nav}")
    own = summarise_arm("paper-v2", book.nav_rows, trades=book.trades, costs=book.cumulative_costs)
    table = benchmark_table(closes, sessions, deposit_rate=Decimal("0.03"))
    print(
        f"paper-v2 return={own['cumulative_return']} trades={own['trades']} costs={own['costs_gbp']} drawdown={own['max_drawdown']}"
    )
    for arm in table["arms"]:
        print(
            f"benchmark {arm['name']} return={arm['cumulative_return']} trades={arm['trades']} "
            f"costs={arm['costs_gbp']} drawdown={arm['max_drawdown']}"
        )


if __name__ == "__main__":
    main()

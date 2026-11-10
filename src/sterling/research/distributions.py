"""Ex-date entitlement and payment-date cash for distributing funds.

On the ex-date the holder accrues a receivable. It counts in NAV and cannot
be spent. On the payment session the receivable becomes cash. Reinvestment,
if the series asks for it, waits until the next session and pays dealing
costs. A missing pay date uses the 15th XLON session after the ex-date, which
is longer than the observed XSTR gaps, so the cash is not assumed early.
"""

from __future__ import annotations

import json
from datetime import date
from importlib.resources import files

from sterling.research.calendar import WeekdayCalendar
from sterling.research.errors import ResearchError

PAY_LAG_SESSIONS = 15


def payment_session(calendar: WeekdayCalendar, ex_date: date, pay_date: date | None) -> date:
    """Session on which the distribution becomes spendable cash."""
    if pay_date is None:
        session = ex_date
        for _ in range(PAY_LAG_SESSIONS):
            session = calendar.next_trading_day(session)
        return session
    if pay_date < ex_date:
        raise ResearchError("distribution pay date is before the ex-date")
    if calendar.is_trading_day(pay_date):
        return pay_date
    return calendar.next_trading_day(pay_date)


def reinvest_session(calendar: WeekdayCalendar, paid_on: date) -> date:
    """Next session after cash is received. Not the payment session itself."""
    return calendar.next_trading_day(paid_on)


def xstr_pay_dates() -> dict[date, date]:
    resource = files("sterling.research").joinpath("data/xstr-pay-dates-v0.json")
    loaded = json.loads(resource.read_text(encoding="utf-8"))
    rows = loaded.get("payments")
    if not isinstance(rows, list):
        raise ResearchError("XSTR pay-date file has no payments list")
    out: dict[date, date] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ResearchError("XSTR pay-date row is not an object")
        ex = date.fromisoformat(str(row["ex_date"]))
        pay = date.fromisoformat(str(row["pay_date"]))
        out[ex] = pay
    return out

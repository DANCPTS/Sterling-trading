"""Europe/London day boundary across the 2026 DST transitions.

2026-03-29 is the spring-forward Sunday. 2026-10-25 is the fall-back Sunday.
Timestamps below are absolute instants with explicit offsets.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.errors import OutOfOrderTimestamp
from sterling.ledger.calendar import london_date
from sterling.ledger.ledger import Ledger
from sterling.rules.model import DeskRules
from tests.support import fee

GMT = UTC
BST = timezone(timedelta(hours=1))


def test_london_civil_dates_around_the_transitions() -> None:
    assert london_date(datetime(2026, 3, 28, 23, 30, tzinfo=GMT)) == date(2026, 3, 28)
    assert london_date(datetime(2026, 3, 29, 0, 0, tzinfo=GMT)) == date(2026, 3, 29)
    assert london_date(datetime(2026, 3, 29, 0, 30, tzinfo=GMT)) == date(2026, 3, 29)
    # 01:30 UTC is 02:30 BST after the gap. It is still 29 March.
    assert london_date(datetime(2026, 3, 29, 1, 30, tzinfo=GMT)) == date(2026, 3, 29)
    assert london_date(datetime(2026, 10, 25, 0, 30, tzinfo=BST)) == date(2026, 10, 25)
    assert london_date(datetime(2026, 10, 25, 1, 30, tzinfo=BST)) == date(2026, 10, 25)
    assert london_date(datetime(2026, 10, 25, 1, 30, tzinfo=GMT)) == date(2026, 10, 25)
    assert london_date(datetime(2026, 10, 26, 0, 0, tzinfo=GMT)) == date(2026, 10, 26)


def test_spring_forward_opens_a_new_day_at_midnight(rules: DeskRules) -> None:
    ledger = Ledger("500.00")
    before = datetime(2026, 3, 28, 23, 45, tzinfo=GMT)
    fee(ledger, event_id="fee-28", timestamp=before, fee_type="commission", amount="2.00")
    march_28 = ledger.daily_metrics(before, rules)
    assert march_28.london_date == date(2026, 3, 28)
    assert march_28.day_opening_equity_gbp == Decimal("500.00")
    assert march_28.day_loss_gbp == Decimal("2.00")

    midnight = datetime(2026, 3, 29, 0, 0, tzinfo=GMT)
    ledger.capture_day_baseline(midnight)
    march_29 = ledger.daily_metrics(midnight, rules)
    assert march_29.london_date == date(2026, 3, 29)
    assert march_29.day_opening_equity_gbp == Decimal("498.00")
    assert march_29.day_loss_gbp == Decimal("0.00")
    assert ledger.daily_metrics(before, rules) == march_28


def test_fallback_hour_shares_one_baseline(rules: DeskRules) -> None:
    # 01:30+01 is 00:30 UTC. 01:40+01 is 00:40 UTC. 01:30+00 is 01:30 UTC, later the same London date.
    first = datetime(2026, 10, 25, 1, 30, tzinfo=BST)
    during = datetime(2026, 10, 25, 1, 40, tzinfo=BST)
    second = datetime(2026, 10, 25, 1, 30, tzinfo=GMT)
    ledger = Ledger("500.00")
    ledger.capture_day_baseline(first)
    fee(ledger, event_id="fee-fallback", timestamp=during, fee_type="commission", amount="1.00")
    ledger.capture_day_baseline(second)
    metrics = ledger.daily_metrics(second, rules)
    assert metrics.london_date == date(2026, 10, 25)
    assert metrics.day_opening_equity_gbp == Decimal("500.00")
    assert metrics.day_loss_gbp == Decimal("1.00")
    assert ledger.view().baselines == (("2026-10-25", Decimal("500.00")),)

    next_day = datetime(2026, 10, 26, 0, 0, tzinfo=GMT)
    ledger.capture_day_baseline(next_day)
    opened = ledger.daily_metrics(next_day, rules)
    assert opened.day_opening_equity_gbp == Decimal("499.00")
    assert opened.day_loss_gbp == Decimal("0.00")
    assert [day for day, _equity in ledger.view().baselines] == ["2026-10-25", "2026-10-26"]


def test_fallback_instants_are_ordered_by_absolute_time() -> None:
    later = datetime(2026, 10, 25, 1, 30, tzinfo=GMT)
    earlier = datetime(2026, 10, 25, 1, 30, tzinfo=BST)
    assert earlier < later
    ledger = Ledger("500.00")
    ledger.capture_day_baseline(later)
    with pytest.raises(OutOfOrderTimestamp):
        fee(ledger, event_id="fee-early", timestamp=earlier, fee_type="commission", amount="1.00")
    assert ledger.view().cash_gbp == Decimal("500.00")
    assert ledger.view().baselines == (("2026-10-25", Decimal("500.00")),)


def test_dst_baseline_survives_restart(rules: DeskRules, tmp_path: Path) -> None:
    ledger = Ledger.open_primary_account(rules)
    before = datetime(2026, 3, 28, 23, 45, tzinfo=GMT)
    fee(ledger, event_id="fee-28", timestamp=before, fee_type="commission", amount="2.00")
    ledger.capture_day_baseline(datetime(2026, 3, 29, 0, 0, tzinfo=GMT))
    path = tmp_path / "dst.jsonl"
    ledger.save(path)
    loaded = Ledger.load(path)
    assert loaded.view() == ledger.view()
    assert [day for day, _equity in loaded.view().baselines] == ["2026-03-28", "2026-03-29"]
    assert loaded.daily_metrics(before, rules).day_loss_gbp == Decimal("2.00")
    assert "2026-11-06" not in {day for day, _equity in loaded.view().baselines}

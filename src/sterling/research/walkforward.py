"""Causal date splits for walk-forward research.

A test window starts the day after its training window ends. The helper
rejects an overlapping split so a look-ahead window cannot be described as
out of sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sterling.research.calendar import add_months
from sterling.research.errors import ResearchError


@dataclass(frozen=True, slots=True)
class DateSplit:
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def assert_causal_split(split: DateSplit) -> None:
    if split.train_start > split.train_end or split.test_start > split.test_end:
        raise ResearchError("walk-forward window is empty")
    if split.test_start <= split.train_end:
        raise ResearchError("test window overlaps training; that split looks ahead")


def walk_forward_splits(
    start: date,
    end: date,
    *,
    train_months: int,
    test_months: int,
    step_months: int | None = None,
) -> tuple[DateSplit, ...]:
    if train_months < 1 or test_months < 1:
        raise ResearchError("walk-forward lengths must be at least one month")
    step = test_months if step_months is None else step_months
    if step < 1:
        raise ResearchError("walk-forward step must be at least one month")
    splits: list[DateSplit] = []
    train_start = start
    while True:
        train_end = add_months(train_start, train_months) - timedelta(days=1)
        test_start = train_end + timedelta(days=1)
        test_end = add_months(test_start, test_months) - timedelta(days=1)
        if test_end > end or train_end < train_start:
            break
        split = DateSplit(train_start, train_end, test_start, test_end)
        assert_causal_split(split)
        splits.append(split)
        train_start = add_months(train_start, step)
    return tuple(splits)

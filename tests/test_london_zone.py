"""Europe/London must resolve when the operating system has no zoneinfo files."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


def test_europe_london_uses_tzdata_when_the_system_path_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import tzdata

    assert tzdata.__name__ == "tzdata"
    monkeypatch.setenv("PYTHONTZPATH", "")
    seen: list[str] = []
    real = zoneinfo._common.load_tzdata

    def spy(key: str):
        seen.append(key)
        return real(key)

    monkeypatch.setattr(zoneinfo._common, "load_tzdata", spy)
    zoneinfo.reset_tzpath()
    ZoneInfo.clear_cache()
    try:
        assert zoneinfo.TZPATH == ()
        london = ZoneInfo.no_cache("Europe/London")
        assert seen == ["Europe/London"]
        assert london.key == "Europe/London"
        winter = datetime(2026, 1, 15, 12, tzinfo=UTC)
        summer = datetime(2026, 7, 15, 12, tzinfo=UTC)
        assert winter.astimezone(london).utcoffset() == timedelta(0)
        assert summer.astimezone(london).utcoffset() == timedelta(hours=1)
    finally:
        ZoneInfo.clear_cache()
        monkeypatch.undo()
        zoneinfo.reset_tzpath()

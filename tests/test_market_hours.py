from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.market_hours import is_market_open

_IST = ZoneInfo("Asia/Kolkata")


def test_open_during_a_weekday_session() -> None:
    # Monday, 11:30 IST.
    assert is_market_open(datetime(2026, 7, 20, 11, 30, tzinfo=_IST)) is True


def test_boundaries_are_inclusive_open_exclusive_close() -> None:
    monday = 20
    assert is_market_open(datetime(2026, 7, monday, 9, 15, tzinfo=_IST)) is True
    assert is_market_open(datetime(2026, 7, monday, 9, 14, 59, tzinfo=_IST)) is False
    assert is_market_open(datetime(2026, 7, monday, 15, 29, 59, tzinfo=_IST)) is True
    assert is_market_open(datetime(2026, 7, monday, 15, 30, tzinfo=_IST)) is False


def test_closed_on_saturday_and_sunday() -> None:
    assert is_market_open(datetime(2026, 7, 18, 11, 30, tzinfo=_IST)) is False  # Saturday
    assert is_market_open(datetime(2026, 7, 19, 11, 30, tzinfo=_IST)) is False  # Sunday


def test_converts_a_non_ist_timezone_before_checking() -> None:
    # 06:00 UTC == 11:30 IST -- still open.
    assert is_market_open(datetime(2026, 7, 20, 6, 0, tzinfo=ZoneInfo("UTC"))) is True

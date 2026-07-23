from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.candle_service import CandleService


class FakeUpstox:
    """Small fake recording which half of Upstox's split candle API was requested."""

    def __init__(self) -> None:
        self.historical_calls: list[dict[str, str]] = []
        self.intraday_calls: list[dict[str, str]] = []

    async def get_historical_candle(self, access_token, instrument_key, **kwargs):
        self.historical_calls.append(kwargs)
        return {
            "data": {
                "candles": [
                    ["2026-07-22T09:20:00+05:30", 101, 104, 100, 103, 20, 7],
                    ["2026-07-22T09:15:00+05:30", 100, 102, 99, 101, 10, 6],
                ]
            }
        }

    async def get_intraday_candle(self, access_token, instrument_key, **kwargs):
        self.intraday_calls.append(kwargs)
        return {
            "data": {
                "candles": [
                    ["2026-07-23T09:15:00+05:30", 103, 105, 102, 104, 30, 8],
                    ["bad", "not-a-number", 0, 0, 0],
                ]
            }
        }


@pytest.mark.anyio
async def test_merges_normalizes_and_sorts_historical_and_intraday_candles():
    fake = FakeUpstox()
    result = await CandleService(fake).get_candles(
        "token",
        instrument_key="NSE_INDEX|Nifty 50",
        unit="minutes",
        interval=5,
        from_date=date(2026, 7, 22),
        to_date=date(2026, 7, 23),
        now=datetime(2026, 7, 23, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert [row["close"] for row in result["candles"]] == [101.0, 103.0, 104.0]
    assert result["candles"][0]["volume"] == 10
    assert result["candles"][0]["open_interest"] == 6.0
    assert fake.historical_calls[0]["to_date"] == "2026-07-22"
    assert len(fake.intraday_calls) == 1


@pytest.mark.anyio
async def test_completed_range_does_not_request_intraday_data():
    fake = FakeUpstox()
    await CandleService(fake).get_candles(
        "token",
        instrument_key="NSE_INDEX|Nifty 50",
        unit="days",
        interval=1,
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 22),
        now=datetime(2026, 7, 23, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert len(fake.historical_calls) == 1
    assert fake.intraday_calls == []

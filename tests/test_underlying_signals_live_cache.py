from __future__ import annotations

from datetime import date

import pytest

from app.services import underlying_signals_service
from app.services.underlying_signals_service import (
    Candle,
    UnderlyingSignalsService,
    _aggregate_minute_candles,
)


@pytest.fixture(autouse=True)
def _clear_module_level_cache():
    # _minute_series checks a module-level 60s in-memory cache before doing anything else, keyed
    # only on (interval, underlying_key, today) -- without clearing it, these tests would share a
    # cache entry across each other (same underlying/day/interval) and never actually reach the
    # fakes below after the first test populates it. Same reset test_routes.py's own _client()
    # fixture already does for exactly this reason.
    underlying_signals_service._CACHE = {}
    yield
    underlying_signals_service._CACHE = {}


def _row(timestamp: str, open_: float, high: float, low: float, close: float, volume: float = 100.0) -> dict:
    return {
        "timestamp": timestamp,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "open_interest": 0.0,
    }


class TestAggregateMinuteCandles:
    def test_groups_rows_into_five_minute_buckets(self) -> None:
        rows = [
            _row("2026-07-24T09:15:00+05:30", 100.0, 101.0, 99.0, 100.5, volume=10),
            _row("2026-07-24T09:16:00+05:30", 100.5, 102.0, 100.0, 101.5, volume=20),
            _row("2026-07-24T09:17:00+05:30", 101.5, 101.8, 101.0, 101.2, volume=15),
            _row("2026-07-24T09:20:00+05:30", 105.0, 106.0, 104.5, 105.5, volume=5),
        ]

        result = _aggregate_minute_candles(rows, 5)

        assert len(result) == 2
        first = result[0]
        assert first.timestamp == "2026-07-24T09:15:00+05:30"
        assert first.open == 100.0  # first row in the bucket
        assert first.high == 102.0  # max across the bucket
        assert first.low == 99.0  # min across the bucket
        assert first.close == 101.2  # last row in the bucket
        assert first.volume == 45.0  # sum across the bucket
        assert result[1].timestamp == "2026-07-24T09:20:00+05:30"

    def test_still_forming_bucket_aggregates_partial_rows(self) -> None:
        # Only 2 of 5 minutes have arrived for the 09:15 bucket -- matches Upstox's own
        # intraday endpoint returning a partial latest bar, not something to special-case away.
        rows = [
            _row("2026-07-24T09:15:00+05:30", 100.0, 101.0, 99.0, 100.5),
            _row("2026-07-24T09:16:00+05:30", 100.5, 102.0, 100.0, 101.5),
        ]

        result = _aggregate_minute_candles(rows, 5)

        assert len(result) == 1
        assert result[0].close == 101.5

    def test_bucket_timestamp_format_matches_upstox_raw_format_exactly(self) -> None:
        # This must match _parse_candles' raw Upstox string format character-for-character, or
        # _merge_candles would create a duplicate bucket instead of overriding the REST one.
        rows = [_row("2026-07-24T09:17:32+05:30", 100.0, 101.0, 99.0, 100.5)]

        result = _aggregate_minute_candles(rows, 5)

        assert result[0].timestamp == "2026-07-24T09:15:00+05:30"

    def test_malformed_rows_are_skipped(self) -> None:
        rows = [
            _row("2026-07-24T09:15:00+05:30", 100.0, 101.0, 99.0, 100.5),
            {"timestamp": "not-a-timestamp", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            {"timestamp": "2026-07-24T09:16:00+05:30", "open": "garbage", "high": 1, "low": 1, "close": 1, "volume": 1},
        ]

        result = _aggregate_minute_candles(rows, 5)

        assert len(result) == 1
        assert result[0].close == 100.5

    def test_empty_input_returns_empty_list(self) -> None:
        assert _aggregate_minute_candles([], 5) == []


class _FakeUpstoxService:
    def __init__(self, *, minute_candles: list[list[object]]) -> None:
        self._minute_candles = minute_candles

    async def get_historical_candle(self, access_token, instrument_key, *, unit, interval, to_date, from_date=None):
        return {"status": "success", "data": {"candles": self._minute_candles}}

    async def get_intraday_candle(self, access_token, instrument_key, *, unit, interval):
        return {"status": "success", "data": {"candles": []}}


class _FakeCandleCacheStore:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = rows or []
        self.saved: list[dict] = []

    def load(self, instrument_key, unit, interval, *, from_timestamp, to_timestamp):
        return list(self._rows)

    def save(self, instrument_key, unit, interval, candles):
        self.saved.extend(candles)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_minute_series_overrides_rest_bucket_with_fresher_live_aggregate() -> None:
    # REST intraday/historical only knows about a stale 09:15 bar; the live cache has two more
    # recent one-minute ticks that complete a fresher picture of that same bucket.
    rest_candles = [["2026-07-24T09:15:00+05:30", 100.0, 100.5, 99.5, 100.0, 10, 0]]
    live_rows = [
        _row("2026-07-24T09:15:00+05:30", 100.0, 100.5, 99.5, 100.2, volume=10),
        _row("2026-07-24T09:17:00+05:30", 100.2, 103.0, 100.0, 102.5, volume=30),
    ]
    cache_store = _FakeCandleCacheStore(live_rows)
    service = UnderlyingSignalsService(
        _FakeUpstoxService(minute_candles=rest_candles), candle_cache_store=cache_store,
    )

    result = await service._minute_series(
        "token", "NSE_INDEX|Nifty 50", interval="5",
        lookback_days=6, today=date(2026, 7, 24), yesterday=date(2026, 7, 23),
    )

    assert len(result) == 1
    bucket = result[0]
    assert bucket.timestamp == "2026-07-24T09:15:00+05:30"
    # The live-aggregated close (102.5, reflecting both live ticks) wins over REST's stale 100.0.
    assert bucket.close == 102.5
    assert bucket.high == 103.0
    assert bucket.volume == 40.0


@pytest.mark.anyio
async def test_minute_series_falls_back_to_rest_only_when_cache_store_has_no_rows_yet() -> None:
    rest_candles = [["2026-07-24T09:15:00+05:30", 100.0, 100.5, 99.5, 100.0, 10, 0]]
    cache_store = _FakeCandleCacheStore(rows=[])  # fresh backend restart, no ticks yet
    service = UnderlyingSignalsService(
        _FakeUpstoxService(minute_candles=rest_candles), candle_cache_store=cache_store,
    )

    result = await service._minute_series(
        "token", "NSE_INDEX|Nifty 50", interval="5",
        lookback_days=6, today=date(2026, 7, 24), yesterday=date(2026, 7, 23),
    )

    assert len(result) == 1
    assert result[0].close == 100.0  # unchanged REST value


@pytest.mark.anyio
async def test_minute_series_works_without_a_cache_store_at_all() -> None:
    rest_candles = [["2026-07-24T09:15:00+05:30", 100.0, 100.5, 99.5, 100.0, 10, 0]]
    service = UnderlyingSignalsService(_FakeUpstoxService(minute_candles=rest_candles))

    result = await service._minute_series(
        "token", "NSE_INDEX|Nifty 50", interval="5",
        lookback_days=6, today=date(2026, 7, 24), yesterday=date(2026, 7, 23),
    )

    assert len(result) == 1
    assert result[0].close == 100.0

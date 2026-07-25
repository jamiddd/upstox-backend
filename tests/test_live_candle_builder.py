from __future__ import annotations

from app.services.live_candle_builder import (
    LiveCandleBuilder,
    feed_candle_to_cache_row,
    merge_live_one_minute_candle,
)
from app.services.upstox_market_feed_client import FeedCandle, FeedTick


def _candle(timestamp_millis: int, open_: float, high: float, low: float, close: float, volume: int = 100) -> FeedCandle:
    return FeedCandle(timestamp_millis=timestamp_millis, open=open_, high=high, low=low, close=close, volume=volume)


def test_ltp_updates_current_minute_close_high_and_low() -> None:
    previous = _candle(120_000, 100.0, 102.0, 99.0, 101.0)

    higher = merge_live_one_minute_candle(
        previous, FeedTick(instrument_key="NSE_INDEX|Nifty 50", ltp=103.0, last_trade_time_millis=135_000),
    )
    lower = merge_live_one_minute_candle(
        higher, FeedTick(instrument_key="NSE_INDEX|Nifty 50", ltp=98.0, last_trade_time_millis=140_000),
    )

    assert lower.open == 100.0
    assert lower.high == 103.0
    assert lower.low == 98.0
    assert lower.close == 98.0


def test_first_tick_in_new_minute_starts_a_new_candle() -> None:
    previous = _candle(120_000, 100.0, 102.0, 99.0, 101.0)

    result = merge_live_one_minute_candle(
        previous, FeedTick(instrument_key="NSE_INDEX|Nifty 50", ltp=105.0, last_trade_time_millis=180_500),
    )

    assert result == _candle(180_000, 105.0, 105.0, 105.0, 105.0, volume=0)


def test_matching_upstox_candle_supplies_open_range_and_volume_while_ltp_supplies_close() -> None:
    supplied = _candle(180_000, 104.0, 106.0, 103.0, 105.0, volume=1_250)

    result = merge_live_one_minute_candle(
        None,
        FeedTick(
            instrument_key="NSE_INDEX|Nifty 50",
            ltp=107.0,
            last_trade_time_millis=190_000,
            one_minute_candle=supplied,
        ),
    )

    assert result == _candle(180_000, 104.0, 107.0, 103.0, 107.0, volume=1_250)


def test_missing_ltp_or_trade_time_falls_back_to_supplied_or_previous() -> None:
    # This is the routine case for indices, which have no real trades -- lastTradeTimeMillis is
    # essentially never populated for them.
    previous = _candle(120_000, 100.0, 102.0, 99.0, 101.0)
    supplied = _candle(180_000, 105.0, 106.0, 104.0, 105.5)

    assert merge_live_one_minute_candle(
        previous, FeedTick(instrument_key="k", ltp=None, last_trade_time_millis=190_000, one_minute_candle=supplied),
    ) == supplied
    assert merge_live_one_minute_candle(
        previous, FeedTick(instrument_key="k", ltp=103.0, last_trade_time_millis=None, one_minute_candle=None),
    ) == previous
    assert merge_live_one_minute_candle(
        None, FeedTick(instrument_key="k", ltp=None, last_trade_time_millis=None, one_minute_candle=None),
    ) is None


def test_feed_candle_to_cache_row_uses_ist_offset_iso_timestamp() -> None:
    # 1_721_873_700_000 ms == 2024-07-25T02:15:00Z == 2024-07-25T07:45:00+05:30.
    candle = _candle(1_721_873_700_000, 100.0, 101.0, 99.5, 100.5, volume=500)

    row = feed_candle_to_cache_row(candle)

    assert row["timestamp"] == "2024-07-25T07:45:00+05:30"
    assert row == {
        "timestamp": row["timestamp"],
        "open": 100.0,
        "high": 101.0,
        "low": 99.5,
        "close": 100.5,
        "volume": 500,
        "open_interest": 0.0,
    }


def test_live_candle_builder_reports_previous_candle_completed_on_new_bucket() -> None:
    completed: list[tuple[str, FeedCandle]] = []
    builder = LiveCandleBuilder(on_candle_completed=lambda key, candle: completed.append((key, candle)))

    builder.handle_tick(FeedTick(instrument_key="A", ltp=100.0, last_trade_time_millis=60_000))
    builder.handle_tick(FeedTick(instrument_key="A", ltp=101.0, last_trade_time_millis=90_000))
    assert completed == []  # still the same minute

    builder.handle_tick(FeedTick(instrument_key="A", ltp=102.0, last_trade_time_millis=120_500))

    assert len(completed) == 1
    key, candle = completed[0]
    assert key == "A"
    assert candle.close == 101.0  # the just-finished minute's last price
    assert builder.latest("A").timestamp_millis == 120_000


def test_live_candle_builder_tracks_multiple_instruments_independently() -> None:
    builder = LiveCandleBuilder()

    builder.handle_tick(FeedTick(instrument_key="A", ltp=100.0, last_trade_time_millis=60_000))
    builder.handle_tick(FeedTick(instrument_key="B", ltp=200.0, last_trade_time_millis=60_000))

    assert builder.latest("A").close == 100.0
    assert builder.latest("B").close == 200.0


def test_clear_drops_in_progress_state() -> None:
    builder = LiveCandleBuilder()
    builder.handle_tick(FeedTick(instrument_key="A", ltp=100.0, last_trade_time_millis=60_000))

    builder.clear("A")

    assert builder.latest("A") is None

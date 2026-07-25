from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.services.upstox_market_feed_client import FeedCandle, FeedTick

_IST = ZoneInfo("Asia/Kolkata")
_ONE_MINUTE_MILLIS = 60_000


def merge_live_one_minute_candle(
    previous: Optional[FeedCandle],
    tick: FeedTick,
) -> Optional[FeedCandle]:
    """Applies a feed tick to the current one-minute candle. Upstox's `I1` OHLC object can refresh
    only around minute boundaries even though LTPC changes arrive continuously, so using that
    object verbatim makes a live series appear to move once per minute. The OHLC object remains
    the authoritative base (especially for volume), while every timestamped LTP updates
    close/high/low.

    Direct port of the Android app's own `FeedTick.kt#mergeLiveOneMinuteCandle` -- same behavior,
    including its documented characteristic that a tick missing `ltp`/`last_trade_time_millis`
    (as routinely happens for indices, which have no real trades) falls back to whatever supplied
    candle or previous candle is available rather than fabricating a bucket. Ported as-is rather
    than re-derived, since this exact logic has already been proven correct in production.
    """
    price = tick.ltp if tick.ltp is not None and math.isfinite(tick.ltp) and tick.ltp > 0.0 else None
    trade_time = (
        tick.last_trade_time_millis
        if tick.last_trade_time_millis is not None and tick.last_trade_time_millis > 0
        else None
    )
    if price is None or trade_time is None:
        return tick.one_minute_candle or previous

    bucket = trade_time - trade_time % _ONE_MINUTE_MILLIS
    supplied = tick.one_minute_candle
    if supplied is not None:
        supplied_bucket = supplied.timestamp_millis - supplied.timestamp_millis % _ONE_MINUTE_MILLIS
        if supplied_bucket != bucket:
            supplied = None
    current = previous if previous is not None and previous.timestamp_millis == bucket else None

    if current is not None:
        return FeedCandle(
            timestamp_millis=current.timestamp_millis,
            open=current.open,
            high=max(current.high, supplied.high if supplied else price, price),
            low=min(current.low, supplied.low if supplied else price, price),
            close=price,
            volume=max(current.volume, supplied.volume if supplied else 0),
        )
    if supplied is not None:
        return FeedCandle(
            timestamp_millis=bucket,
            open=supplied.open,
            high=max(supplied.high, price),
            low=min(supplied.low, price),
            close=price,
            volume=supplied.volume,
        )
    return FeedCandle(timestamp_millis=bucket, open=price, high=price, low=price, close=price, volume=0)


def feed_candle_to_cache_row(candle: FeedCandle) -> dict[str, Any]:
    """Shapes a `FeedCandle` into the row format `CandleCacheStore.save()`/`CandleService`
    already use elsewhere in this backend -- an ISO-8601 timestamp with the IST offset, matching
    what Upstox's own REST candle rows are normalized to (see `candle_service.py
    ._normalize_candles`), so cached live-built rows and REST-sourced rows are interchangeable."""
    timestamp = datetime.fromtimestamp(candle.timestamp_millis / 1000, tz=_IST)
    return {
        "timestamp": timestamp.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "open_interest": 0.0,
    }


class LiveCandleBuilder:
    """Maintains the current in-progress one-minute candle per instrument key from a live tick
    stream, and reports the previous minute's candle as "completed" the moment a tick's bucket
    moves past it -- so a caller (e.g. `FeedSubscriptionManager`'s tick dispatch) can persist each
    finished bar into `CandleCacheStore` without needing its own bucket-tracking logic.

    One instance is shared across all instruments the market feed is currently subscribed to --
    state is a plain dict keyed by instrument_key, not one builder per instrument, since ticks for
    many different instruments arrive interleaved on the same feed connection.
    """

    def __init__(self, *, on_candle_completed: Optional[Callable[[str, FeedCandle], None]] = None) -> None:
        self._current: dict[str, FeedCandle] = {}
        self._on_candle_completed = on_candle_completed

    def handle_tick(self, tick: FeedTick) -> Optional[FeedCandle]:
        previous = self._current.get(tick.instrument_key)
        updated = merge_live_one_minute_candle(previous, tick)
        if updated is None:
            return None
        if (
            previous is not None
            and previous.timestamp_millis != updated.timestamp_millis
            and self._on_candle_completed is not None
        ):
            self._on_candle_completed(tick.instrument_key, previous)
        self._current[tick.instrument_key] = updated
        return updated

    def latest(self, instrument_key: str) -> Optional[FeedCandle]:
        return self._current.get(instrument_key)

    def clear(self, instrument_key: str) -> None:
        """Drops in-progress state for an instrument no longer being watched -- otherwise a stale
        candle could be reported as "current" if the same instrument is resubscribed later in a
        different session, long after its last real tick."""
        self._current.pop(instrument_key, None)

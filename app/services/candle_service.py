from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.services.upstox_service import UpstoxService
from app.services.candle_cache_store import CandleCacheStore

_IST = ZoneInfo("Asia/Kolkata")


async def _no_candles() -> dict[str, Any]:
    """Placeholder awaitable for `asyncio.gather` when a caller doesn't need one of the two
    branches (e.g. `to_date` is entirely in the past, so there's no intraday leg) -- its result is
    never read (see the `needs_historical`/`needs_intraday` guards around each branch below)."""
    return {}


class CandleService:
    """Build the mobile chart's chronological candle series from Upstox V3 data.

    Upstox deliberately separates completed sessions from the current session. This service
    hides that upstream detail from clients, merges both sources, and de-duplicates by timestamp
    so the Android chart always receives one stable, oldest-first series.
    """

    def __init__(self, upstox: UpstoxService, cache_store: CandleCacheStore | None = None) -> None:
        """Create a chart candle service backed by the shared Upstox REST integration."""
        self.upstox = upstox
        self.cache_store = cache_store

    async def get_candles(
        self,
        access_token: str,
        *,
        instrument_key: str,
        unit: str,
        interval: int,
        from_date: date,
        to_date: date,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return normalized candles for an inclusive date range.

        Historical data is requested only through yesterday because Upstox's historical endpoint
        never returns the still-forming session. The intraday endpoint is added only when the
        requested range includes today.
        """
        today = (now or datetime.now(_IST)).astimezone(_IST).date()
        yesterday = today - timedelta(days=1)
        rows_by_timestamp: dict[str, dict[str, Any]] = {}
        if self.cache_store is not None:
            cached = self.cache_store.load(
                instrument_key,
                unit,
                interval,
                from_timestamp=f"{from_date.isoformat()}T00:00:00",
                to_timestamp=f"{(to_date + timedelta(days=1)).isoformat()}T00:00:00",
            )
            rows_by_timestamp.update({row["timestamp"]: row for row in cached})

        historical_to = min(to_date, yesterday)
        needs_historical = from_date <= historical_to
        needs_intraday = from_date <= today <= to_date

        # Independent Upstox endpoints (completed sessions vs. the still-forming one) -- fired
        # concurrently instead of sequentially, since a chart's full load previously paid two
        # chained round-trips back to back for no reason (and this compounds badly when several
        # charts load at once, e.g. the 3-pane layout).
        historical, intraday = await asyncio.gather(
            self.upstox.get_historical_candle(
                access_token,
                instrument_key,
                unit=unit,
                interval=str(interval),
                to_date=historical_to.isoformat(),
                from_date=from_date.isoformat(),
            )
            if needs_historical
            else _no_candles(),
            self.upstox.get_intraday_candle(
                access_token,
                instrument_key,
                unit=unit,
                interval=str(interval),
            )
            if needs_intraday
            else _no_candles(),
        )

        if needs_historical:
            normalized_historical = _normalize_candles(historical)
            rows_by_timestamp.update(normalized_historical)
            if self.cache_store is not None:
                self.cache_store.save(
                    instrument_key, unit, interval, list(normalized_historical.values()),
                )

        if needs_intraday:
            normalized_intraday = _normalize_candles(intraday)
            rows_by_timestamp.update(normalized_intraday)
            if self.cache_store is not None:
                self.cache_store.save(
                    instrument_key, unit, interval, list(normalized_intraday.values()),
                )

        candles = [rows_by_timestamp[key] for key in sorted(rows_by_timestamp)]
        expected_latest_date = _expected_latest_trading_date(
            min(to_date, today),
            local_now=(now or datetime.now(_IST)).astimezone(_IST),
        )
        latest_candle_date = _latest_candle_date(candles)
        return {
            "instrument_key": instrument_key,
            "unit": unit,
            "interval": interval,
            "timezone": "Asia/Kolkata",
            "candles": candles,
            "expected_latest_trading_date": expected_latest_date.isoformat(),
            "latest_candle_date": latest_candle_date.isoformat() if latest_candle_date else None,
            "is_stale": latest_candle_date is None or latest_candle_date < expected_latest_date,
        }


def _normalize_candles(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Convert Upstox's positional candle arrays into named, timestamp-keyed objects."""
    data = payload.get("data")
    raw_candles = data.get("candles") if isinstance(data, dict) else None
    if not isinstance(raw_candles, list):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for row in raw_candles:
        if not isinstance(row, list) or len(row) < 5 or not isinstance(row[0], str):
            continue
        try:
            timestamp = datetime.fromisoformat(row[0]).isoformat()
            candle = {
                "timestamp": timestamp,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": int(row[5]) if len(row) > 5 and row[5] is not None else 0,
                "open_interest": float(row[6]) if len(row) > 6 and row[6] is not None else 0.0,
            }
        except (TypeError, ValueError):
            continue
        normalized[timestamp] = candle
    return normalized


def _expected_latest_trading_date(requested_to: date, *, local_now: datetime) -> date:
    candidate = requested_to
    # Before today's normal session begins there cannot be a current-session candle yet.
    if candidate == local_now.date() and local_now.time() < time(9, 15):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _latest_candle_date(candles: list[dict[str, Any]]) -> date | None:
    for candle in reversed(candles):
        try:
            return datetime.fromisoformat(candle["timestamp"]).astimezone(_IST).date()
        except (KeyError, TypeError, ValueError):
            continue
    return None

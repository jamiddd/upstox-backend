from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.services.upstox_service import UpstoxService

_IST = ZoneInfo("Asia/Kolkata")


class CandleService:
    """Build the mobile chart's chronological candle series from Upstox V3 data.

    Upstox deliberately separates completed sessions from the current session. This service
    hides that upstream detail from clients, merges both sources, and de-duplicates by timestamp
    so the Android chart always receives one stable, oldest-first series.
    """

    def __init__(self, upstox: UpstoxService) -> None:
        """Create a chart candle service backed by the shared Upstox REST integration."""
        self.upstox = upstox

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

        historical_to = min(to_date, yesterday)
        if from_date <= historical_to:
            historical = await self.upstox.get_historical_candle(
                access_token,
                instrument_key,
                unit=unit,
                interval=str(interval),
                to_date=historical_to.isoformat(),
                from_date=from_date.isoformat(),
            )
            rows_by_timestamp.update(_normalize_candles(historical))

        if from_date <= today <= to_date:
            intraday = await self.upstox.get_intraday_candle(
                access_token,
                instrument_key,
                unit=unit,
                interval=str(interval),
            )
            rows_by_timestamp.update(_normalize_candles(intraday))

        candles = [rows_by_timestamp[key] for key in sorted(rows_by_timestamp)]
        return {
            "instrument_key": instrument_key,
            "unit": unit,
            "interval": interval,
            "timezone": "Asia/Kolkata",
            "candles": candles,
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

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from time import monotonic
from typing import Any, Optional

from app.services.upstox_service import UpstoxService

# How far apart LTP has to be from a candidate level for it to still count as "near" it -- see
# _nearest_level. Expressed as a percent of LTP so it scales sensibly across very different
# underlyings (a few points on NIFTY vs. a few rupees on a low-priced stock).
_NEAREST_LEVEL_TOLERANCE_PERCENT = 0.15

# The classic opening-range window: the first 15 minutes of the session, i.e. the first three
# 5-minute candles (9:15-9:30 IST).
_OPENING_RANGE_CANDLES = 3


@dataclass
class Candle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class _CacheEntry:
    expires_at: float
    value: Any


_CACHE: dict[tuple[Any, ...], _CacheEntry] = {}


class UnderlyingSignalsService:
    """Computes glanceable technical-analysis tags for the underlying -- 9 EMA (5m and 15m),
    ATR(14), today's opening-range position, and proximity to a "crucial level" (previous-day
    H/L/C, classic pivots, or a round psychological number) -- shown to the user just before they
    place a strike order. See docs/MAIN_SCREEN_API.md's "Underlying Signals" section.

    Deliberately computed on the *underlying's* own price action, not the option contract being
    traded: an option premium is dominated by theta decay and IV changes rather than the
    underlying's own trend/momentum, so an EMA/ATR/opening-range reading on the premium itself
    would be meaningless for this purpose.
    """

    def __init__(self, upstox_service: UpstoxService) -> None:
        self.upstox = upstox_service

    async def get_signals(self, access_token: str, *, underlying_key: str) -> dict[str, Any]:
        today = date.today()
        yesterday = today - timedelta(days=1)

        candles_5m = await self._minute_series(
            access_token, underlying_key, interval="5", lookback_days=6, today=today, yesterday=yesterday,
        )
        candles_15m = await self._minute_series(
            access_token, underlying_key, interval="15", lookback_days=10, today=today, yesterday=yesterday,
        )
        daily_candles = await self._daily_series(access_token, underlying_key, today=today, yesterday=yesterday)
        round_step = await self._round_step(access_token, underlying_key)
        ltp = await self._ltp(access_token, underlying_key)

        ema9_5m = _ema([c.close for c in candles_5m], period=9)
        ema9_15m = _ema([c.close for c in candles_15m], period=9)
        atr14_5m = _atr(candles_5m, period=14)

        opening_range_high, opening_range_low = _opening_range(
            _todays_candles(candles_5m), window_candles=_OPENING_RANGE_CANDLES,
        )

        prev_day = daily_candles[-1] if daily_candles else None
        pivots = _pivots(prev_day.high, prev_day.low, prev_day.close) if prev_day else {}

        round_below, round_above = _round_levels(ltp, round_step)

        levels: dict[str, float] = {}
        if prev_day:
            levels["Prev Day High"] = prev_day.high
            levels["Prev Day Low"] = prev_day.low
            levels["Prev Day Close"] = prev_day.close
        if pivots:
            levels["Pivot"] = pivots["p"]
            levels["R1 Pivot"] = pivots["r1"]
            levels["R2 Pivot"] = pivots["r2"]
            levels["S1 Pivot"] = pivots["s1"]
            levels["S2 Pivot"] = pivots["s2"]
        if round_step > 0:
            levels[f"{round_below:g} level"] = round_below
            levels[f"{round_above:g} level"] = round_above

        nearest_level = _nearest_level(ltp, levels, tolerance_percent=_NEAREST_LEVEL_TOLERANCE_PERCENT)

        ema9_5m_position = _position(ltp, ema9_5m)
        ema9_15m_position = _position(ltp, ema9_15m)
        opening_range_position = _range_position(ltp, opening_range_high, opening_range_low)

        tags = _build_tags(
            ltp=ltp,
            ema9_5m_value=ema9_5m,
            ema9_5m_position=ema9_5m_position,
            ema9_15m_value=ema9_15m,
            ema9_15m_position=ema9_15m_position,
            atr14_5m=atr14_5m,
            opening_range_high=opening_range_high,
            opening_range_low=opening_range_low,
            opening_range_position=opening_range_position,
            nearest_level=nearest_level,
        )

        return {
            "ltp": ltp,
            "ema9_5m": {"value": _round_or_none(ema9_5m), "position": ema9_5m_position},
            "ema9_15m": {"value": _round_or_none(ema9_15m), "position": ema9_15m_position},
            "atr14_5m": _round_or_none(atr14_5m),
            "opening_range": {
                "window_minutes": 15,
                "high": _round_or_none(opening_range_high),
                "low": _round_or_none(opening_range_low),
                "position": opening_range_position,
            },
            "previous_day": {
                "high": _round_or_none(prev_day.high) if prev_day else None,
                "low": _round_or_none(prev_day.low) if prev_day else None,
                "close": _round_or_none(prev_day.close) if prev_day else None,
            },
            "pivots": {key: round(value, 2) for key, value in pivots.items()},
            "round_step": round_step,
            "nearest_level": nearest_level,
            "tags": tags,
        }

    async def _minute_series(
        self,
        access_token: str,
        underlying_key: str,
        *,
        interval: str,
        lookback_days: int,
        today: date,
        yesterday: date,
    ) -> list[Candle]:
        # Cached candle-derived data only changes when a new candle closes, so a 60s TTL is
        # plenty -- see the class doc's "computed on the underlying" note for why this is safe to
        # share across every caller asking about the same underlying+day.
        cache_key = (f"candles_{interval}m", underlying_key, today.isoformat())
        cached = _cache_get(cache_key)
        if cached is not None:
            return [Candle(**row) for row in cached]

        from_date = (today - timedelta(days=lookback_days)).isoformat()
        historical = await self.upstox.get_historical_candle(
            access_token,
            underlying_key,
            unit="minutes",
            interval=interval,
            to_date=yesterday.isoformat(),
            from_date=from_date,
        )
        intraday = await self.upstox.get_intraday_candle(
            access_token, underlying_key, unit="minutes", interval=interval,
        )
        candles = _merge_candles(_parse_candles(historical), _parse_candles(intraday))
        _cache_set(cache_key, [asdict(candle) for candle in candles], ttl_seconds=60.0)
        return candles

    async def _daily_series(
        self,
        access_token: str,
        underlying_key: str,
        *,
        today: date,
        yesterday: date,
    ) -> list[Candle]:
        # Only needs to change once a day (a new daily candle closes at session end), but a 1h
        # TTL (not a full day) is simpler and safe enough against date rollover mid-cache.
        cache_key = ("candles_1d", underlying_key, today.isoformat())
        cached = _cache_get(cache_key)
        if cached is not None:
            return [Candle(**row) for row in cached]

        from_date = (today - timedelta(days=20)).isoformat()
        payload = await self.upstox.get_historical_candle(
            access_token,
            underlying_key,
            unit="days",
            interval="1",
            to_date=yesterday.isoformat(),
            from_date=from_date,
        )
        candles = _parse_candles(payload)
        _cache_set(cache_key, [asdict(candle) for candle in candles], ttl_seconds=3600.0)
        return candles

    async def _round_step(self, access_token: str, underlying_key: str) -> float:
        cache_key = ("round_step", underlying_key)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        payload = await self.upstox.get_option_contracts(access_token, underlying_key)
        data = payload.get("data")
        strikes: set[float] = set()
        if isinstance(data, list):
            for contract in data:
                if not isinstance(contract, dict):
                    continue
                value = contract.get("strike_price")
                if isinstance(value, (int, float)):
                    strikes.add(float(value))
        step = _mode_gap(sorted(strikes))
        _cache_set(cache_key, step, ttl_seconds=600.0)
        return step

    async def _ltp(self, access_token: str, underlying_key: str) -> float:
        payload = await self.upstox.get_quotes(access_token, underlying_key)
        data = payload.get("data")
        if not isinstance(data, dict):
            return 0.0
        for quote in data.values():
            if not isinstance(quote, dict):
                continue
            value = quote.get("last_price")
            if isinstance(value, (int, float)):
                return float(value)
        return 0.0


def _cache_get(key: tuple[Any, ...]) -> Optional[Any]:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    if entry.expires_at <= monotonic():
        _CACHE.pop(key, None)
        return None
    return entry.value


def _cache_set(key: tuple[Any, ...], value: Any, *, ttl_seconds: float) -> None:
    _CACHE[key] = _CacheEntry(expires_at=monotonic() + ttl_seconds, value=value)


def _parse_candles(payload: dict[str, Any]) -> list[Candle]:
    """Upstox's historical-candle response: `{"data": {"candles": [[timestamp, open, high, low,
    close, volume, oi], ...]}}`, newest-first. Returned here sorted oldest-first, since every
    computation below (EMA, ATR, opening range) reads the series chronologically.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("candles")
    if not isinstance(rows, list):
        return []

    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            candles.append(
                Candle(
                    timestamp=str(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                ),
            )
        except (TypeError, ValueError):
            continue
    candles.sort(key=lambda candle: candle.timestamp)
    return candles


def _merge_candles(*candle_lists: list[Candle]) -> list[Candle]:
    """Merges the completed-session (historical) and still-forming (intraday) series, deduping by
    timestamp -- the two calls' date ranges can overlap by exactly one session at the boundary.
    """
    merged: dict[str, Candle] = {}
    for candles in candle_lists:
        for candle in candles:
            merged[candle.timestamp] = candle
    return sorted(merged.values(), key=lambda candle: candle.timestamp)


def _todays_candles(candles: list[Candle]) -> list[Candle]:
    if not candles:
        return []
    latest_date = candles[-1].timestamp[:10]
    return [candle for candle in candles if candle.timestamp[:10] == latest_date]


def _ema(values: list[float], *, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = value * alpha + ema * (1.0 - alpha)
    return ema


def _true_range(prev_close: float, high: float, low: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _atr(candles: list[Candle], *, period: int) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    true_ranges = [
        _true_range(candles[i - 1].close, candles[i].high, candles[i].low) for i in range(1, len(candles))
    ]
    if len(true_ranges) < period:
        return None
    # Wilder's smoothing: seed with a simple average of the first `period` true ranges, then
    # smooth every subsequent one in -- the standard ATR calculation.
    atr = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        atr = (atr * (period - 1) + true_range) / period
    return atr


def _opening_range(candles: list[Candle], *, window_candles: int) -> tuple[Optional[float], Optional[float]]:
    window = candles[:window_candles]
    if not window:
        return (None, None)
    return (max(candle.high for candle in window), min(candle.low for candle in window))


def _pivots(high: float, low: float, close: float) -> dict[str, float]:
    pivot = (high + low + close) / 3.0
    return {
        "p": pivot,
        "r1": 2 * pivot - low,
        "s1": 2 * pivot - high,
        "r2": pivot + (high - low),
        "s2": pivot - (high - low),
    }


def _round_levels(ltp: float, step: float) -> tuple[float, float]:
    if step <= 0 or ltp <= 0:
        return (0.0, 0.0)
    below = (ltp // step) * step
    return (below, below + step)


def _mode_gap(sorted_strikes: list[float]) -> float:
    """The most common gap between consecutive strikes -- more robust than just the first gap
    against a stray illiquid/missing strike widening one particular gap.
    """
    if len(sorted_strikes) < 2:
        return 0.0
    gaps = [round(b - a, 4) for a, b in zip(sorted_strikes, sorted_strikes[1:]) if b > a]
    if not gaps:
        return 0.0
    return Counter(gaps).most_common(1)[0][0]


def _nearest_level(ltp: float, levels: dict[str, float], *, tolerance_percent: float) -> Optional[dict[str, Any]]:
    if ltp <= 0:
        return None

    best_label: Optional[str] = None
    best_value: Optional[float] = None
    best_distance_percent: Optional[float] = None
    for label, value in levels.items():
        if value <= 0:
            continue
        distance_percent = abs(ltp - value) / ltp * 100.0
        if best_distance_percent is None or distance_percent < best_distance_percent:
            best_label, best_value, best_distance_percent = label, value, distance_percent

    if best_label is None or best_value is None or best_distance_percent is None:
        return None
    if best_distance_percent > tolerance_percent:
        return None
    return {
        "label": best_label,
        "value": round(best_value, 2),
        "distance_percent": round(best_distance_percent, 3),
    }


def _position(ltp: float, value: Optional[float]) -> Optional[str]:
    if value is None or ltp <= 0:
        return None
    if ltp > value:
        return "above"
    if ltp < value:
        return "below"
    return "at"


def _range_position(ltp: float, high: Optional[float], low: Optional[float]) -> Optional[str]:
    if high is None or low is None or ltp <= 0:
        return None
    if ltp > high:
        return "above"
    if ltp < low:
        return "below"
    return "inside"


def _round_or_none(value: Optional[float]) -> Optional[float]:
    return round(value, 2) if value is not None else None


def _build_tags(
    *,
    ltp: float,
    ema9_5m_value: Optional[float],
    ema9_5m_position: Optional[str],
    ema9_15m_value: Optional[float],
    ema9_15m_position: Optional[str],
    atr14_5m: Optional[float],
    opening_range_high: Optional[float],
    opening_range_low: Optional[float],
    opening_range_position: Optional[str],
    nearest_level: Optional[dict[str, Any]],
) -> list[str]:
    """Builds the ready-to-render tag strings -- every directional tag (EMA above/below, opening
    range above/below, a nearby level) spells out the absolute point distance from LTP, not just
    the direction, e.g. "Above 5m EMA9 by 12.30" rather than a bare "Above 5m EMA9" -- the app's
    user wants the magnitude at a glance, not just the sign.
    """
    tags: list[str] = []
    if ema9_5m_position and ema9_5m_value is not None:
        tags.append(f"{ema9_5m_position.capitalize()} 5m EMA9 by {abs(ltp - ema9_5m_value):.2f}")
    if ema9_15m_position and ema9_15m_value is not None:
        tags.append(f"{ema9_15m_position.capitalize()} 15m EMA9 by {abs(ltp - ema9_15m_value):.2f}")
    if atr14_5m is not None:
        tags.append(f"ATR {round(atr14_5m, 1):g}")
    if opening_range_position == "above" and opening_range_high is not None:
        tags.append(f"Above opening range by {ltp - opening_range_high:.2f}")
    elif opening_range_position == "below" and opening_range_low is not None:
        tags.append(f"Below opening range by {opening_range_low - ltp:.2f}")
    elif opening_range_position == "inside":
        tags.append("Inside opening range")
    if nearest_level:
        distance = abs(ltp - nearest_level["value"])
        tags.append(f"Near {nearest_level['label']} by {distance:.2f}")
    return tags

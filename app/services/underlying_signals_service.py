from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from time import monotonic
from typing import Any, Optional

from app.core.exceptions import UpstoxApiError
from app.services.oi_analysis_service import OIAnalysisService
from app.services.upstox_service import UpstoxService

# How far apart LTP has to be from a candidate level for it to still count as "near" it -- see
# _nearest_level. Expressed as a percent of LTP so it scales sensibly across very different
# underlyings (a few points on NIFTY vs. a few rupees on a low-priced stock).
_NEAREST_LEVEL_TOLERANCE_PERCENT = 0.15

# The classic opening-range window: the first 15 minutes of the session, i.e. the first three
# 5-minute candles (9:15-9:30 IST).
_OPENING_RANGE_CANDLES = 3

# Opening-range "measured move" target levels, as multiples of the OR's own size (high - low),
# projected beyond whichever side price has broken out of -- OR Target 1 = breakout side +/- 0.5x
# the OR, Target 2 = 1x, Target 3 = 1.5x, Target 4 = 2x. A breakout is genuinely bullish/bearish,
# but each of these is also a level price has historically tended to stall/reverse at -- see
# _nearest_or_target's doc comment for why that matters to the tag shown.
_OR_TARGET_MULTIPLIERS = (0.5, 1.0, 1.5, 2.0)

# PCR bias thresholds -- see _pcr_bias. A PCR this high means far more puts are open than calls,
# read as bullish (heavy put writing = traders selling downside protection, i.e. not expecting a
# fall); this low means the opposite.
_PCR_BULLISH_THRESHOLD = 1.2
_PCR_BEARISH_THRESHOLD = 0.8

# How many listed strikes on *each side* of ATM count as "near" for PCR/OI support/resistance --
# see _near_atm_strikes. This app is a scalping tool; OI concentrated 15 strikes away is noise for
# that time frame, and would otherwise dominate both the support/resistance pick and the PCR ratio
# (a single huge far-OTM print skews the whole-chain PCR far more than it skews the actual trade).
_NEAR_ATM_STRIKE_COUNT = 5

# LTP within this many *absolute* points of today's session open is a "no-trade zone" -- the app's
# user gets whipsawed trading right around the open before price has picked a direction. Dynamic,
# not a single fixed number -- see _no_trade_zone_points -- since a flat point-count can't be right
# for every underlying (15 points is a very different fraction of a low-priced stock than of
# NIFTY) or even for the same underlying across different volatility regimes. Points here (not a
# percent of LTP like _NEAREST_LEVEL_TOLERANCE_PERCENT) is still the right *unit*, though -- this
# is a caution about a specific instant (the open print), not a general proximity-to-a-level read.
_NO_TRADE_ZONE_ATR_MULTIPLIER = 0.75
_NO_TRADE_ZONE_MIN_POINTS = 5.0
# Used only when ATR itself isn't available yet (not enough candle history) -- the original fixed
# value this whole thing used to be, kept as a safe fallback rather than disabling the caution
# entirely.
_NO_TRADE_ZONE_FALLBACK_POINTS = 15.0

# Rolling 5-minute-change history (see _record_and_diff) -- how far back a snapshot can be and
# still count as "5 minutes ago" (a tolerance band, not an exact match, since polling is ~60s but
# not perfectly metronomic), and how long a snapshot is kept around at all before being pruned.
_FIVE_MIN_MIN_SECONDS = 240.0
_FIVE_MIN_MAX_SECONDS = 360.0
_HISTORY_WINDOW_SECONDS = 600.0

# SENSEX has no actively-traded futures contract on Upstox (unlike NIFTY/BANKNIFTY on NSE) -- per
# explicit product decision, VWAP for SENSEX always uses Nifty's own futures contract instead.
# Upstox's own key convention (matches every other *_INDEX entry already in this codebase, e.g.
# "NSE_INDEX|Nifty 50") -- not independently verified against a live instrument master, so
# _is_sensex also falls back to a symbol-text match in case this guess is wrong.
_SENSEX_UNDERLYING_KEY = "BSE_INDEX|SENSEX"
_NIFTY_UNDERLYING_KEY = "NSE_INDEX|Nifty 50"


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


@dataclass
class _OiSummary:
    """Everything derived from one OI Analysis call -- see UnderlyingSignalsService._oi_analysis.
    All fields default to None (never a partial mix -- either the whole call succeeded or it
    didn't) so a single check on any one field tells you whether OI data is available at all.
    """

    pcr: Optional[float] = None
    max_pain: Optional[float] = None
    # The strike with the single highest put OI -- heavy put writing there reads as a support
    # level (put writers are betting price stays above it, and will defend that bet).
    support_strike: Optional[float] = None
    support_oi: Optional[float] = None
    # The strike with the single highest call OI -- the resistance-level mirror of support_strike.
    resistance_strike: Optional[float] = None
    resistance_oi: Optional[float] = None


@dataclass
class _HistorySnapshot:
    """One poll's worth of the values that get a "5-minute change" suffix -- see
    _record_and_diff. `vwap_distance`/`level_distance` store the *distance* (|LTP - VWAP| / |LTP -
    level|), not VWAP/the level's own value -- see _record_and_diff's doc comment for why."""

    timestamp: float
    atr: Optional[float]
    vwap_distance: Optional[float]
    level_distance: Optional[float]
    pcr: Optional[float]
    support_strike: Optional[float]
    support_oi: Optional[float]
    resistance_strike: Optional[float]
    resistance_oi: Optional[float]
    atm_straddle: Optional[float]


@dataclass
class _HistoryDeltas:
    """`current - (whatever was recorded ~5 minutes ago)` for each metric in _HistorySnapshot,
    from _record_and_diff -- `None` for any metric with no in-band snapshot to compare against, or
    where either side is itself `None`."""

    atr: Optional[float] = None
    vwap_distance: Optional[float] = None
    level_distance: Optional[float] = None
    pcr: Optional[float] = None
    support_oi: Optional[float] = None
    resistance_oi: Optional[float] = None
    atm_straddle: Optional[float] = None


# Keyed by (underlying_key, expiry_date) -- expiry-specific metrics (PCR/OI/ATM straddle) can't be
# meaningfully diffed across an expiry switch, so the whole history resets together with one key
# scheme rather than keeping two.
_HISTORY: dict[tuple[str, Optional[str]], list[_HistorySnapshot]] = {}


_CACHE: dict[tuple[Any, ...], _CacheEntry] = {}


class UnderlyingSignalsService:
    """Computes glanceable technical-analysis tags for the underlying -- 9 EMA (5m and 15m),
    ATR(14), today's opening-range position, proximity to a "crucial level" (previous-day H/L/C,
    classic pivots, or a round psychological number), and (when [expiry_date] is given) PCR-based
    bias, Max Pain pull direction, and OI support/resistance strikes from open-interest data --
    shown to the user just before they place a strike order. PCR and OI support/resistance are
    computed only from strikes near the money (see _near_atm_strikes), since this app is a
    scalping tool and OI parked far from the money is noise, not signal, for that time frame. See
    docs/MAIN_SCREEN_API.md's "Underlying Signals" section.

    Deliberately computed on the *underlying's* own price action, not the option contract being
    traded: an option premium is dominated by theta decay and IV changes rather than the
    underlying's own trend/momentum, so an EMA/ATR/opening-range reading on the premium itself
    would be meaningless for this purpose.
    """

    def __init__(self, upstox_service: UpstoxService) -> None:
        self.upstox = upstox_service

    async def get_signals(
        self,
        access_token: str,
        *,
        underlying_key: str,
        expiry_date: Optional[str] = None,
        underlying_symbol: Optional[str] = None,
    ) -> dict[str, Any]:
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

        todays_candles_5m = _todays_candles(candles_5m)
        opening_range_high, opening_range_low = _opening_range(
            todays_candles_5m, window_candles=_OPENING_RANGE_CANDLES,
        )
        today_open = todays_candles_5m[0].open if todays_candles_5m else None
        # Dynamic, not a flat constant -- scales with how volatile this session actually is (a
        # quiet low-ATR session gets a tighter buffer than a volatile one), floored so it never
        # shrinks to near-nothing, falling back to the original fixed value only when ATR itself
        # isn't available yet (not enough candle history).
        no_trade_zone_points = (
            max(atr14_5m * _NO_TRADE_ZONE_ATR_MULTIPLIER, _NO_TRADE_ZONE_MIN_POINTS)
            if atr14_5m is not None
            else _NO_TRADE_ZONE_FALLBACK_POINTS
        )
        no_trade_zone = _is_near_day_open(ltp, today_open, tolerance_points=no_trade_zone_points)

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

        nearest_or_target = _nearest_or_target(
            ltp,
            opening_range_high=opening_range_high,
            opening_range_low=opening_range_low,
            opening_range_position=opening_range_position,
            tolerance_percent=_NEAREST_LEVEL_TOLERANCE_PERCENT,
        )

        oi_summary = await self._oi_analysis(access_token, underlying_key, expiry_date, today, ltp)
        pcr_bias = _pcr_bias(oi_summary.pcr)
        max_pain_pull = _max_pain_pull(ltp, oi_summary.max_pain)

        vwap, vwap_position = await self._vwap_signal(
            access_token, underlying_key=underlying_key, underlying_symbol=underlying_symbol,
            today=today, yesterday=yesterday,
        )

        atm_straddle = await self._fetch_atm_straddle(access_token, underlying_key, expiry_date, ltp)

        # 5-minute-change tracking for ATR/VWAP-distance/level-distance/PCR/OI-support/
        # OI-resistance/ATM-straddle -- see _record_and_diff's doc comment for exactly what each
        # one measures (VWAP/level track the *distance* closing in or pulling away, not their own
        # value; OI support/resistance are strike-matched, ATM straddle deliberately isn't).
        level_distance = abs(ltp - nearest_level["value"]) if nearest_level else None
        vwap_distance = abs(ltp - vwap) if vwap is not None else None
        deltas = _record_and_diff(
            (underlying_key, expiry_date),
            atr=atr14_5m,
            vwap_distance=vwap_distance,
            level_distance=level_distance,
            pcr=oi_summary.pcr,
            support_strike=oi_summary.support_strike,
            support_oi=oi_summary.support_oi,
            resistance_strike=oi_summary.resistance_strike,
            resistance_oi=oi_summary.resistance_oi,
            atm_straddle=atm_straddle,
            now=monotonic(),
        )

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
            nearest_or_target=nearest_or_target,
            pcr=oi_summary.pcr,
            pcr_bias=pcr_bias,
            max_pain=oi_summary.max_pain,
            max_pain_pull=max_pain_pull,
            oi_support_strike=oi_summary.support_strike,
            oi_resistance_strike=oi_summary.resistance_strike,
            vwap_value=vwap,
            vwap_position=vwap_position,
            today_open=today_open,
            no_trade_zone=no_trade_zone,
            no_trade_zone_points=no_trade_zone_points,
            atm_straddle=atm_straddle,
            atr_delta=deltas.atr,
            vwap_distance_delta=deltas.vwap_distance,
            level_distance_delta=deltas.level_distance,
            pcr_delta=deltas.pcr,
            support_oi_delta=deltas.support_oi,
            resistance_oi_delta=deltas.resistance_oi,
            atm_straddle_delta=deltas.atm_straddle,
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
            "today_open": _round_or_none(today_open),
            "no_trade_zone": no_trade_zone,
            "nearest_level": nearest_level,
            "nearest_or_target": nearest_or_target,
            "pcr": {"value": _round_or_none(oi_summary.pcr), "bias": pcr_bias} if oi_summary.pcr is not None else None,
            "max_pain": (
                {"value": _round_or_none(oi_summary.max_pain), "pull": max_pain_pull}
                if oi_summary.max_pain is not None
                else None
            ),
            "oi_support": (
                {"value": oi_summary.support_strike, "oi": oi_summary.support_oi}
                if oi_summary.support_strike is not None
                else None
            ),
            "oi_resistance": (
                {"value": oi_summary.resistance_strike, "oi": oi_summary.resistance_oi}
                if oi_summary.resistance_strike is not None
                else None
            ),
            "vwap": {"value": _round_or_none(vwap), "position": vwap_position} if vwap is not None else None,
            "tags": tags,
        }

    async def _oi_analysis(
        self,
        access_token: str,
        underlying_key: str,
        expiry_date: Optional[str],
        today: date,
        ltp: float,
    ) -> "_OiSummary":
        """PCR/max pain/OI support-resistance for [expiry_date], reusing the existing
        OIAnalysisService (its own 60s cache applies, nothing duplicated here) -- an all-`None`
        [_OiSummary] if no expiry was given (a contract-free underlying, or a caller that doesn't
        have one yet) or Upstox's OI endpoints fail, same "degrade this one piece, not the whole
        response" posture as MainScreenService.summary's funds-unavailable handling.

        `pcr` and the support/resistance strikes are all computed **only** from the
        [_NEAR_ATM_STRIKE_COUNT] strikes on each side of ATM (see _near_atm_strikes) -- this app is
        a scalping tool, so OI concentrated far from the money is noise, not signal, for either
        purpose. `max_pain` is left as Upstox's own whole-chain value, deliberately unrestricted --
        "max pain" is inherently a whole-chain concept (the strike that minimizes aggregate option
        writer payout across every strike), so narrowing its inputs would just make it a different,
        wrong number rather than a more scalping-relevant one.
        """
        if not expiry_date:
            return _OiSummary()
        try:
            analysis = await OIAnalysisService(self.upstox).get_analysis(
                access_token,
                instrument_key=underlying_key,
                expiry=expiry_date,
                date=today.isoformat(),
                change_interval=1,
                bucket_interval=60,
            )
        except UpstoxApiError:
            return _OiSummary()

        max_pain = analysis.get("max_pain", {}).get("max_pain")
        max_pain = float(max_pain) if isinstance(max_pain, (int, float)) else None

        strike_rows = analysis.get("oi", {}).get("call_put_oi_data_list")
        near_atm_rows = _near_atm_strikes(
            strike_rows if isinstance(strike_rows, list) else [],
            ltp,
            count=_NEAR_ATM_STRIKE_COUNT,
        )
        pcr = _local_pcr(near_atm_rows)
        support_strike, support_oi, resistance_strike, resistance_oi = _oi_support_resistance(near_atm_rows)

        return _OiSummary(
            pcr=pcr,
            max_pain=max_pain,
            support_strike=support_strike,
            support_oi=support_oi,
            resistance_strike=resistance_strike,
            resistance_oi=resistance_oi,
        )

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

    async def _vwap_signal(
        self,
        access_token: str,
        *,
        underlying_key: str,
        underlying_symbol: Optional[str],
        today: date,
        yesterday: date,
    ) -> tuple[Optional[float], Optional[str]]:
        """Session VWAP computed from the underlying's own futures contract (the index itself has
        no traded volume). Degrades quietly to `(None, None)` -- never raises into get_signals --
        whenever a futures contract can't be resolved or its candle/LTP fetch fails, e.g. no
        `underlying_symbol` given, no futures market for this underlying at all (true for most
        individual equities), or an Upstox API failure.
        """
        futures_key = await self._futures_instrument_key(
            access_token, underlying_key=underlying_key, underlying_symbol=underlying_symbol,
        )
        if not futures_key:
            return None, None
        try:
            futures_candles = await self._minute_series(
                access_token, futures_key, interval="5", lookback_days=2, today=today, yesterday=yesterday,
            )
            futures_ltp = await self._ltp(access_token, futures_key)
        except UpstoxApiError:
            return None, None
        vwap = _vwap(futures_candles)
        return vwap, _position(futures_ltp, vwap)

    async def _futures_instrument_key(
        self, access_token: str, *, underlying_key: str, underlying_symbol: Optional[str],
    ) -> Optional[str]:
        """Resolves the current-month futures contract to use for this underlying's VWAP --
        matched by underlying_key (not just symbol text, which could ambiguously match a related
        index's own future, e.g. "NIFTY" also matching FINNIFTY/MIDCPNIFTY), EXCEPT SENSEX (see
        _is_sensex), which always resolves Nifty's own future instead since SENSEX has no futures
        market on Upstox. Returns None -- gracefully, never raises -- whenever a future can't be
        resolved for any reason: no underlying_symbol given (older client), no current-month
        future listed for this underlying at all (true for most individual equities), or the
        resolution API call itself fails.
        """
        if _is_sensex(underlying_key, underlying_symbol):
            search_query, match_key = "NIFTY", _NIFTY_UNDERLYING_KEY
        elif underlying_symbol:
            search_query, match_key = underlying_symbol, underlying_key
        else:
            return None

        cache_key = ("futures_key", underlying_key, date.today().isoformat())
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached.get("key")
        try:
            payload = await self.upstox.search_instruments(
                access_token, query=search_query, segments="FO",
                instrument_types="FUT", expiry="current_month", records=10,
            )
        except UpstoxApiError:
            return None
        rows = payload.get("data")
        matches = [
            row for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and row.get("underlying_key") == match_key
            and isinstance(row.get("instrument_key"), str)
        ]
        resolved = matches[0]["instrument_key"] if matches else None
        _cache_set(cache_key, {"key": resolved}, ttl_seconds=3600.0)
        return resolved

    async def _fetch_atm_straddle(
        self, access_token: str, underlying_key: str, expiry_date: Optional[str], ltp: float,
    ) -> Optional[float]:
        """ATM call premium + ATM put premium (the strike closest to `ltp`), for the "ATM
        Straddle" tag and its own 5-minute change (see _record_and_diff's doc comment for why that
        change is tracked as a plain rolling series, not gated on the strike staying the same).
        `None` -- gracefully, never raises -- whenever `expiry_date` wasn't given (straddle premium
        only exists per-expiry, same gating as PCR/max-pain/OI), `ltp` isn't loaded yet, or the
        option-chain fetch itself fails. Cached briefly (15s, matching
        `MainScreenService._option_chain_live`'s own TTL for this same live per-strike data) since
        this is live-changing, not static.
        """
        if not expiry_date or ltp <= 0:
            return None

        cache_key = ("atm_straddle_chain", underlying_key, expiry_date)
        payload = _cache_get(cache_key)
        if payload is None:
            try:
                payload = await self.upstox.get_option_chain(access_token, underlying_key, expiry_date=expiry_date)
            except UpstoxApiError:
                return None
            _cache_set(cache_key, payload, ttl_seconds=15.0)

        rows = payload.get("data")
        usable = [
            row for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and isinstance(row.get("strike_price"), (int, float))
        ]
        if not usable:
            return None
        atm_row = min(usable, key=lambda row: abs(row["strike_price"] - ltp))
        ce_ltp = _option_ltp(atm_row.get("call_options"))
        pe_ltp = _option_ltp(atm_row.get("put_options"))
        if ce_ltp is None or pe_ltp is None:
            return None
        return ce_ltp + pe_ltp


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


def _is_sensex(underlying_key: str, underlying_symbol: Optional[str]) -> bool:
    if underlying_key == _SENSEX_UNDERLYING_KEY:
        return True
    return bool(underlying_symbol) and underlying_symbol.strip().upper() == "SENSEX"


def _option_ltp(side: Any) -> Optional[float]:
    """Extracts `market_data.ltp` from one option-chain row's `call_options`/`put_options` side --
    mirrors `main_screen_service.py`'s own `_option_side` extraction (not shared/imported across
    services, same "each service calls self.upstox independently" posture used throughout this
    file already)."""
    if not isinstance(side, dict):
        return None
    market_data = side.get("market_data")
    ltp = market_data.get("ltp") if isinstance(market_data, dict) else None
    return float(ltp) if isinstance(ltp, (int, float)) else None


def _vwap(candles: list[Candle]) -> Optional[float]:
    """Session VWAP = cumulative(typical price * volume) / cumulative(volume), today's candles
    only. Typical price = (high+low+close)/3 -- volume traded across a candle's whole range, not
    just its closing tick, same reasoning as ATR using the full high/low, not just closes.
    """
    todays = _todays_candles(candles)
    total_volume = sum(c.volume for c in todays)
    if total_volume <= 0:
        return None
    weighted_sum = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in todays)
    return weighted_sum / total_volume


def _ema(values: list[float], *, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = value * alpha + ema * (1.0 - alpha)
    return ema


def _record_and_diff(
    key: tuple[str, Optional[str]],
    *,
    atr: Optional[float],
    vwap_distance: Optional[float],
    level_distance: Optional[float],
    pcr: Optional[float],
    support_strike: Optional[float],
    support_oi: Optional[float],
    resistance_strike: Optional[float],
    resistance_oi: Optional[float],
    atm_straddle: Optional[float],
    now: float,
) -> _HistoryDeltas:
    """Records the current values as a new snapshot under [key] and returns how much each one has
    changed since ~5 minutes ago (a 4-6 minute tolerance band, not an exact match -- see
    _FIVE_MIN_MIN_SECONDS/_FIVE_MIN_MAX_SECONDS -- since polling is ~60s but not perfectly
    metronomic). `None` for any metric with no in-band snapshot to compare against (e.g. right
    after this underlying/expiry was selected), or where either side is itself `None`.

    `vwap_distance`/`level_distance`: these store |LTP - VWAP| / |LTP - level|, not VWAP/the
    level's own value -- a moving VWAP number by itself isn't actionable, what matters is whether
    price is *approaching or pulling away* from it (or a static level), so a **negative** delta
    here means the distance shrank (price closing in), positive means it grew (pulling away) --
    the sign is about the distance, not VWAP/the level's own direction.

    `support_oi`/`resistance_oi`: only diffed when the matched snapshot's own `support_strike`/
    `resistance_strike` equals the *current* one -- if a different strike has taken over as
    support/resistance since then, comparing their OI numbers wouldn't mean anything (not the same
    thing being measured), so the delta is `None` rather than a misleading number.

    `atm_straddle`: deliberately has **no** such strike-matching gate, unlike support/resistance --
    the ATM strike is *expected* to roll as price moves, and "ATM straddle" is conventionally read
    as a rolling index (whatever's ATM right now), not one fixed strike's own price history.
    """
    history = _HISTORY.setdefault(key, [])
    history[:] = [snapshot for snapshot in history if now - snapshot.timestamp <= _HISTORY_WINDOW_SECONDS]

    in_band = [
        snapshot for snapshot in history
        if _FIVE_MIN_MIN_SECONDS <= now - snapshot.timestamp <= _FIVE_MIN_MAX_SECONDS
    ]
    matched = min(in_band, key=lambda snapshot: abs((now - snapshot.timestamp) - 300.0)) if in_band else None

    def diff(current: Optional[float], previous: Optional[float]) -> Optional[float]:
        if current is None or previous is None:
            return None
        return current - previous

    deltas = _HistoryDeltas()
    if matched is not None:
        deltas.atr = diff(atr, matched.atr)
        deltas.vwap_distance = diff(vwap_distance, matched.vwap_distance)
        deltas.level_distance = diff(level_distance, matched.level_distance)
        deltas.pcr = diff(pcr, matched.pcr)
        deltas.atm_straddle = diff(atm_straddle, matched.atm_straddle)
        if support_strike is not None and matched.support_strike == support_strike:
            deltas.support_oi = diff(support_oi, matched.support_oi)
        if resistance_strike is not None and matched.resistance_strike == resistance_strike:
            deltas.resistance_oi = diff(resistance_oi, matched.resistance_oi)

    history.append(
        _HistorySnapshot(
            timestamp=now,
            atr=atr,
            vwap_distance=vwap_distance,
            level_distance=level_distance,
            pcr=pcr,
            support_strike=support_strike,
            support_oi=support_oi,
            resistance_strike=resistance_strike,
            resistance_oi=resistance_oi,
            atm_straddle=atm_straddle,
        )
    )
    return deltas


def _is_near_day_open(ltp: float, today_open: Optional[float], *, tolerance_points: float) -> bool:
    """Whether LTP is within [tolerance_points] *absolute* points of today's session open -- see
    _NO_TRADE_ZONE_POINTS. `False` (not a no-trade zone) whenever today's open isn't known yet
    (no candles for today) or LTP itself hasn't loaded -- same "missing data means no caution, not
    a false one" posture as every other None-safe helper here.
    """
    if today_open is None or ltp <= 0:
        return False
    return abs(ltp - today_open) <= tolerance_points


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


def _or_targets(anchor: float, or_range: float, *, sign: int) -> dict[str, float]:
    """The four measured-move target levels beyond `anchor` (the opening range's high for an
    upside breakout, its low for a downside one) -- `sign` is +1 for upside (targets *above*
    anchor) or -1 for downside (targets *below*). "OR Target 1" is the nearest (0.5x the OR's own
    size) through "OR Target 4" (2x), matching the ordinal numbering the app's user thinks in.
    """
    return {
        f"OR Target {index}": anchor + sign * multiplier * or_range
        for index, multiplier in enumerate(_OR_TARGET_MULTIPLIERS, start=1)
    }


def _nearest_or_target(
    ltp: float,
    *,
    opening_range_high: Optional[float],
    opening_range_low: Optional[float],
    opening_range_position: Optional[str],
    tolerance_percent: float,
) -> Optional[dict[str, Any]]:
    """Whichever OR measured-move target (see _or_targets) LTP is currently closest to, if
    within [tolerance_percent] -- **only** once price has actually broken out of the opening
    range (`opening_range_position` is "above" or "below"; "inside" or unknown never has a
    target to be near). A breakout past the OR is a genuinely bullish/bearish signal on its own,
    but each of these target levels is also a level price has historically tended to stall or
    reverse at -- so a breakout that's also sitting right on one of them is the same directional
    signal with an added "don't chase this exact level" caution, not a contradiction of it.
    """
    if opening_range_high is None or opening_range_low is None:
        return None
    or_range = opening_range_high - opening_range_low
    if or_range <= 0:
        return None

    if opening_range_position == "above":
        targets = _or_targets(opening_range_high, or_range, sign=1)
    elif opening_range_position == "below":
        targets = _or_targets(opening_range_low, or_range, sign=-1)
    else:
        return None

    return _nearest_level(ltp, targets, tolerance_percent=tolerance_percent)


def _pcr_bias(pcr: Optional[float]) -> Optional[str]:
    """Bullish if enough puts are open relative to calls (heavy put writing reads as traders not
    expecting a fall), bearish the other way, neutral in between -- see _PCR_BULLISH_THRESHOLD/
    _PCR_BEARISH_THRESHOLD. `None` (no tag) if PCR itself is unavailable.
    """
    if pcr is None:
        return None
    if pcr >= _PCR_BULLISH_THRESHOLD:
        return "bullish"
    if pcr <= _PCR_BEARISH_THRESHOLD:
        return "bearish"
    return "neutral"


def _max_pain_pull(ltp: float, max_pain: Optional[float]) -> Optional[str]:
    """Price tends to gravitate toward max pain (the strike where option writers collectively
    lose the least) as expiry approaches -- bullish if LTP is currently below it (expected pull
    up), bearish if above (expected pull down). `None` if max pain itself is unavailable.
    """
    if max_pain is None or ltp <= 0:
        return None
    if ltp < max_pain:
        return "bullish"
    if ltp > max_pain:
        return "bearish"
    return "neutral"


def _near_atm_strikes(strike_rows: list[dict[str, Any]], ltp: float, *, count: int) -> list[dict[str, Any]]:
    """The `count` listed strikes on *each side* of ATM (whichever strike in [strike_rows] is
    nearest to `ltp`), plus ATM itself -- sorted by strike_price first so "each side" means what
    it says. Everything derived from OI (PCR, support/resistance) only ever looks at this
    subset -- see _oi_analysis's doc comment for why the far strikes are excluded entirely rather
    than just down-weighted.
    """
    usable = sorted(
        (row for row in strike_rows if isinstance(row, dict) and isinstance(row.get("strike_price"), (int, float))),
        key=lambda row: row["strike_price"],
    )
    if not usable:
        return []
    atm_index = min(range(len(usable)), key=lambda i: abs(usable[i]["strike_price"] - ltp))
    start = max(0, atm_index - count)
    end = min(len(usable), atm_index + count + 1)
    return usable[start:end]


def _local_pcr(near_atm_rows: list[dict[str, Any]]) -> Optional[float]:
    """Put-call ratio computed from just [near_atm_rows] (see _near_atm_strikes) -- sum of put OI
    over sum of call OI across that subset. `None` if there's no call OI to divide by (empty
    subset, or every row's call_oi missing/zero).
    """
    total_put_oi = 0.0
    total_call_oi = 0.0
    for row in near_atm_rows:
        put_oi = row.get("put_oi")
        if isinstance(put_oi, (int, float)):
            total_put_oi += put_oi
        call_oi = row.get("call_oi")
        if isinstance(call_oi, (int, float)):
            total_call_oi += call_oi
    if total_call_oi <= 0:
        return None
    return total_put_oi / total_call_oi


def _oi_support_resistance(
    strike_rows: list[dict[str, Any]],
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Returns `(support_strike, support_oi, resistance_strike, resistance_oi)` from Upstox's
    per-strike `call_put_oi_data_list` -- support is the strike with the single highest put OI,
    resistance the strike with the single highest call OI (see _OiSummary's doc comment for why).
    All `None` if [strike_rows] is empty or has no usable rows.
    """
    support_strike: Optional[float] = None
    support_oi: Optional[float] = None
    resistance_strike: Optional[float] = None
    resistance_oi: Optional[float] = None

    for row in strike_rows:
        if not isinstance(row, dict):
            continue
        strike = row.get("strike_price")
        if not isinstance(strike, (int, float)):
            continue

        put_oi = row.get("put_oi")
        if isinstance(put_oi, (int, float)) and (support_oi is None or put_oi > support_oi):
            support_oi = float(put_oi)
            support_strike = float(strike)

        call_oi = row.get("call_oi")
        if isinstance(call_oi, (int, float)) and (resistance_oi is None or call_oi > resistance_oi):
            resistance_oi = float(call_oi)
            resistance_strike = float(strike)

    return support_strike, support_oi, resistance_strike, resistance_oi


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
    nearest_or_target: Optional[dict[str, Any]],
    pcr: Optional[float],
    pcr_bias: Optional[str],
    max_pain: Optional[float],
    max_pain_pull: Optional[str],
    oi_support_strike: Optional[float],
    oi_resistance_strike: Optional[float],
    vwap_value: Optional[float],
    vwap_position: Optional[str],
    today_open: Optional[float],
    no_trade_zone: bool,
    no_trade_zone_points: float,
    atm_straddle: Optional[float],
    atr_delta: Optional[float],
    vwap_distance_delta: Optional[float],
    level_distance_delta: Optional[float],
    pcr_delta: Optional[float],
    support_oi_delta: Optional[float],
    resistance_oi_delta: Optional[float],
    atm_straddle_delta: Optional[float],
) -> list[str]:
    """Builds the ready-to-render tag strings -- every directional tag (EMA above/below, opening
    range above/below, a nearby level) spells out the absolute point distance from LTP, not just
    the direction, e.g. "Above 5m EMA9 by 12.30" rather than a bare "Above 5m EMA9" -- the app's
    user wants the magnitude at a glance, not just the sign.

    A breakout that's also sitting on one of the OR's measured-move target levels
    ([nearest_or_target], see _nearest_or_target) folds its caution straight into the same
    "Above/Below opening range" tag -- one line, not a second tag -- still bullish/bearish, just
    flagged as a level price has historically tended to stall or reverse at, not a contradiction
    of the breakout itself. That caution's own distance is signed (LTP minus the target value,
    with an explicit +/-), not absolute -- the sign tells you which side of the exact target
    price currently sits on.

    The PCR/max-pain tags don't start with "Above"/"Below" like every other tag here -- the
    Android client's tag-sentiment classifier (`sentimentForSignalTag`) also recognizes a bare
    "bullish"/"bearish" word anywhere in the text, which is why both are spelled out explicitly
    below rather than reusing the "Above X"/"Below X" phrasing that wouldn't fit either signal.

    The no-trade-zone caution ([no_trade_zone], see _is_near_day_open) is inserted first, ahead
    of every other tag -- it's a warning not to act on the rest of the bulletin right now, so it
    needs to be the first thing the user reads, not buried after several bullish/bearish-looking
    reads that would otherwise seem to say "trade this".

    The two 9 EMA reads (5m and 15m) are folded into a single line rather than two separate tags
    -- the 5m read leads (it's the one meant for scalping timing, per this service's own doc
    comment) and still drives the line's own "Above"/"Below" prefix (so the Android tag-sentiment
    classifier keeps reading it correctly), with the 15m read parenthesized alongside it, same
    "fold a second fact into the same line" pattern the OR-target caution above already uses.
    When only one of the two is available (not enough candle history yet for the other), that one
    stands alone instead, unparenthesized.

    Several tags below carry a trailing 5-minute-change suffix (see _record_and_diff) via
    [_delta_suffix]/[_oi_delta_suffix] -- `None` (no in-band history yet) simply omits it, same
    "missing data means omit" posture as everything else here. For VWAP/nearest-level specifically,
    the delta is the *distance's* own change (negative = price closing in, positive = pulling
    away), not VWAP/the level's own value change -- see _record_and_diff's doc comment for why.
    """
    tags: list[str] = []
    if no_trade_zone and today_open is not None:
        tags.append(f"No-Trade Zone -- within {no_trade_zone_points:g} of Day Open ({today_open:g})")
    have_5m = ema9_5m_position and ema9_5m_value is not None
    have_15m = ema9_15m_position and ema9_15m_value is not None
    if have_5m and have_15m:
        tags.append(
            f"{ema9_5m_position.capitalize()} 5m EMA9 by {abs(ltp - ema9_5m_value):.2f}"
            f" (15m {ema9_15m_position.capitalize()} by {abs(ltp - ema9_15m_value):.2f})"
        )
    elif have_5m:
        tags.append(f"{ema9_5m_position.capitalize()} 5m EMA9 by {abs(ltp - ema9_5m_value):.2f}")
    elif have_15m:
        tags.append(f"{ema9_15m_position.capitalize()} 15m EMA9 by {abs(ltp - ema9_15m_value):.2f}")
    if atr14_5m is not None:
        tags.append(f"ATR {round(atr14_5m, 1):g}{_delta_suffix(atr_delta)}")
    if opening_range_position == "above" and opening_range_high is not None:
        tags.append(f"Above opening range by {ltp - opening_range_high:.2f}{_or_target_caution(ltp, nearest_or_target, 'pullback')}")
    elif opening_range_position == "below" and opening_range_low is not None:
        tags.append(f"Below opening range by {opening_range_low - ltp:.2f}{_or_target_caution(ltp, nearest_or_target, 'bounce')}")
    elif opening_range_position == "inside":
        tags.append("Inside opening range")
    if nearest_level:
        distance = abs(ltp - nearest_level["value"])
        tags.append(f"Near {nearest_level['label']} by {distance:.2f}{_delta_suffix(level_distance_delta)}")
    if pcr is not None and pcr_bias is not None:
        tags.append(f"PCR {pcr:.2f} - {pcr_bias.capitalize()} bias{_delta_suffix(pcr_delta)}")
    if max_pain is not None and max_pain_pull is not None:
        tags.append(f"Max Pain {max_pain:g} by {ltp - max_pain:+.2f} - {max_pain_pull.capitalize()} pull")
    if oi_support_strike is not None:
        tags.append(f"OI Support {oi_support_strike:g} by {ltp - oi_support_strike:+.2f}{_oi_delta_suffix(support_oi_delta, 'Put')}")
    if oi_resistance_strike is not None:
        tags.append(f"OI Resistance {oi_resistance_strike:g} by {ltp - oi_resistance_strike:+.2f}{_oi_delta_suffix(resistance_oi_delta, 'Call')}")
    if vwap_position and vwap_value is not None:
        tags.append(f"{vwap_position.capitalize()} VWAP by {abs(ltp - vwap_value):.2f}{_delta_suffix(vwap_distance_delta)}")
    if atm_straddle is not None:
        tags.append(f"ATM Straddle {atm_straddle:.2f}{_delta_suffix(atm_straddle_delta)}")
    return tags


def _delta_suffix(delta: Optional[float]) -> str:
    """The `" (+X.XX in 5m)"` trailing suffix shared by every plain-value 5-minute-change tag --
    empty string if no delta is available (see _record_and_diff)."""
    return f" ({delta:+.2f} in 5m)" if delta is not None else ""


def _oi_delta_suffix(delta: Optional[float], label: str) -> str:
    """Same idea as [_delta_suffix], but for OI support/resistance -- comma-grouped, no decimals
    (OI is always a whole contract count), and labeled Put/Call explicitly since a bare number
    here would be ambiguous about which side it's counting."""
    return f" ({label} OI {delta:+,.0f} in 5m)" if delta is not None else ""


def _or_target_caution(ltp: float, nearest_or_target: Optional[dict[str, Any]], reversal_word: str) -> str:
    """The `" (near OR Target N by +/-Y.YY, caution: possible pullback/bounce)"` suffix appended
    to the opening-range breakout tag -- empty string if LTP isn't currently near any target.
    """
    if not nearest_or_target:
        return ""
    signed_distance = ltp - nearest_or_target["value"]
    return f" (near {nearest_or_target['label']} by {signed_distance:+.2f}, caution: possible {reversal_word})"

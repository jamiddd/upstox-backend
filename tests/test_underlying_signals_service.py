from __future__ import annotations

from datetime import datetime, timedelta

import anyio
import pytest

from app.services import underlying_signals_service as signals
from app.services.underlying_signals_service import Candle, UnderlyingSignalsService


@pytest.fixture(autouse=True)
def clear_signals_cache() -> None:
    signals._CACHE = {}


# --- pure math helpers -------------------------------------------------------------------


def test_ema_matches_hand_computed_series() -> None:
    """9-period EMA over a simple rising series, hand-verified step by step."""
    values = [float(v) for v in range(10, 21)]  # 10..20, 11 values

    ema = signals._ema(values, period=9)

    assert ema == pytest.approx(16.4294967296)


def test_ema_returns_none_when_series_too_short() -> None:
    assert signals._ema([1.0, 2.0], period=9) is None


def test_atr_matches_hand_computed_wilder_smoothing() -> None:
    """4 candles -> 4 true ranges -> ATR(3) seeded from the first 3, smoothed with the 4th."""
    candles = [
        Candle(timestamp="t0", open=9.0, high=10.0, low=8.0, close=9.0, volume=0.0),
        Candle(timestamp="t1", open=9.0, high=11.0, low=9.0, close=10.0, volume=0.0),
        Candle(timestamp="t2", open=10.0, high=12.0, low=10.0, close=11.0, volume=0.0),
        Candle(timestamp="t3", open=11.0, high=13.0, low=11.0, close=12.0, volume=0.0),
        Candle(timestamp="t4", open=12.0, high=15.0, low=11.0, close=14.0, volume=0.0),
    ]

    atr = signals._atr(candles, period=3)

    # true ranges: 2, 2, 2, 4 -- seed = avg(2,2,2) = 2.0, then smoothed with 4: (2*2+4)/3
    assert atr == pytest.approx(8.0 / 3.0)


def test_atr_returns_none_when_not_enough_candles() -> None:
    candles = [
        Candle(timestamp="t0", open=9.0, high=10.0, low=8.0, close=9.0, volume=0.0),
        Candle(timestamp="t1", open=9.0, high=11.0, low=9.0, close=10.0, volume=0.0),
    ]
    assert signals._atr(candles, period=14) is None


def test_pivots_match_classic_formula() -> None:
    pivots = signals._pivots(high=110.0, low=90.0, close=100.0)

    assert pivots == {"p": 100.0, "r1": 110.0, "s1": 90.0, "r2": 120.0, "s2": 80.0}


def test_opening_range_uses_first_n_candles_of_the_window() -> None:
    candles = [
        Candle(timestamp="t0", open=0.0, high=100.0, low=95.0, close=98.0, volume=0.0),
        Candle(timestamp="t1", open=0.0, high=105.0, low=98.0, close=102.0, volume=0.0),
        Candle(timestamp="t2", open=0.0, high=103.0, low=97.0, close=101.0, volume=0.0),
        Candle(timestamp="t3", open=0.0, high=110.0, low=100.0, close=108.0, volume=0.0),
    ]

    high, low = signals._opening_range(candles, window_candles=3)

    assert (high, low) == (105.0, 95.0)


def test_todays_candles_keeps_only_the_latest_date() -> None:
    candles = [
        Candle(timestamp="2026-07-17T09:15:00+05:30", open=0.0, high=1.0, low=1.0, close=1.0, volume=0.0),
        Candle(timestamp="2026-07-18T09:15:00+05:30", open=0.0, high=2.0, low=2.0, close=2.0, volume=0.0),
        Candle(timestamp="2026-07-18T09:20:00+05:30", open=0.0, high=3.0, low=3.0, close=3.0, volume=0.0),
    ]

    todays = signals._todays_candles(candles)

    assert [c.timestamp for c in todays] == [
        "2026-07-18T09:15:00+05:30",
        "2026-07-18T09:20:00+05:30",
    ]


def test_mode_gap_picks_the_most_common_strike_spacing() -> None:
    strikes = [24800.0, 24850.0, 24900.0, 24950.0, 25100.0]  # gaps: 50, 50, 50, 150

    assert signals._mode_gap(strikes) == 50.0


def test_round_levels_bracket_ltp_at_the_given_step() -> None:
    below, above = signals._round_levels(24930.0, 50.0)

    assert (below, above) == (24900.0, 24950.0)


def test_nearest_level_picks_the_closest_level_within_tolerance() -> None:
    nearest = signals._nearest_level(
        100.0,
        {"A": 100.1, "B": 105.0, "C": 99.5},
        tolerance_percent=0.5,
    )

    assert nearest == {"label": "A", "value": 100.1, "distance_percent": 0.1}


def test_nearest_level_returns_none_when_everything_is_too_far() -> None:
    nearest = signals._nearest_level(100.0, {"X": 110.0}, tolerance_percent=0.15)

    assert nearest is None


def test_position_reports_above_below_and_at() -> None:
    assert signals._position(101.0, 100.0) == "above"
    assert signals._position(99.0, 100.0) == "below"
    assert signals._position(100.0, 100.0) == "at"
    assert signals._position(100.0, None) is None


def test_range_position_reports_above_below_and_inside() -> None:
    assert signals._range_position(111.0, high=110.0, low=90.0) == "above"
    assert signals._range_position(89.0, high=110.0, low=90.0) == "below"
    assert signals._range_position(100.0, high=110.0, low=90.0) == "inside"


def test_pcr_bias_thresholds() -> None:
    assert signals._pcr_bias(1.2) == "bullish"
    assert signals._pcr_bias(1.5) == "bullish"
    assert signals._pcr_bias(0.8) == "bearish"
    assert signals._pcr_bias(0.5) == "bearish"
    assert signals._pcr_bias(1.0) == "neutral"
    assert signals._pcr_bias(None) is None


def test_max_pain_pull_direction() -> None:
    # LTP below max pain -> expected to be pulled up (bullish); above -> pulled down (bearish).
    assert signals._max_pain_pull(24950.0, 25000.0) == "bullish"
    assert signals._max_pain_pull(25050.0, 25000.0) == "bearish"
    assert signals._max_pain_pull(25000.0, 25000.0) == "neutral"
    assert signals._max_pain_pull(25050.0, None) is None


def test_build_tags_composes_readable_short_labels_with_absolute_point_distances() -> None:
    tags = signals._build_tags(
        ltp=25100.0,
        ema9_5m_value=25050.0,
        ema9_5m_position="above",
        ema9_15m_value=25150.0,
        ema9_15m_position="below",
        atr14_5m=42.349,
        opening_range_high=None,
        opening_range_low=None,
        opening_range_position="inside",
        nearest_level={"label": "R1 Pivot", "value": 25086.6, "distance_percent": 0.1},
        nearest_or_target=None,
        pcr=None,
        pcr_bias=None,
        max_pain=None,
        max_pain_pull=None,
        oi_support_strike=None,
        oi_resistance_strike=None,
    )

    assert tags == [
        "Above 5m EMA9 by 50.00",
        "Below 15m EMA9 by 50.00",
        "ATR 42.3",
        "Inside opening range",
        "Near R1 Pivot by 13.40",
    ]


def test_build_tags_reports_opening_range_breakout_distance() -> None:
    above = signals._build_tags(
        ltp=25110.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=None,
        ema9_15m_position=None,
        atr14_5m=None,
        opening_range_high=25100.0,
        opening_range_low=25000.0,
        opening_range_position="above",
        nearest_level=None,
        nearest_or_target=None,
        pcr=None,
        pcr_bias=None,
        max_pain=None,
        max_pain_pull=None,
        oi_support_strike=None,
        oi_resistance_strike=None,
    )
    below = signals._build_tags(
        ltp=24990.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=None,
        ema9_15m_position=None,
        atr14_5m=None,
        opening_range_high=25100.0,
        opening_range_low=25000.0,
        opening_range_position="below",
        nearest_level=None,
        nearest_or_target=None,
        pcr=None,
        pcr_bias=None,
        max_pain=None,
        max_pain_pull=None,
        oi_support_strike=None,
        oi_resistance_strike=None,
    )

    assert above == ["Above opening range by 10.00"]
    assert below == ["Below opening range by 10.00"]


def test_build_tags_folds_or_target_caution_into_the_opening_range_tag() -> None:
    # LTP is exactly on the target here -> signed distance is 0.00, which should still render
    # with an explicit "+" (Python's :+.2f format spec), not a bare "0.00".
    on_target = signals._build_tags(
        ltp=25110.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=None,
        ema9_15m_position=None,
        atr14_5m=None,
        opening_range_high=25100.0,
        opening_range_low=25000.0,
        opening_range_position="above",
        nearest_level=None,
        nearest_or_target={"label": "OR Target 1", "value": 25110.0, "distance_percent": 0.0},
        pcr=None,
        pcr_bias=None,
        max_pain=None,
        max_pain_pull=None,
        oi_support_strike=None,
        oi_resistance_strike=None,
    )
    # LTP is 2 points *past* the target (overshot it) -> positive signed distance.
    past_target = signals._build_tags(
        ltp=25112.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=None,
        ema9_15m_position=None,
        atr14_5m=None,
        opening_range_high=25100.0,
        opening_range_low=25000.0,
        opening_range_position="above",
        nearest_level=None,
        nearest_or_target={"label": "OR Target 1", "value": 25110.0, "distance_percent": 0.01},
        pcr=None,
        pcr_bias=None,
        max_pain=None,
        max_pain_pull=None,
        oi_support_strike=None,
        oi_resistance_strike=None,
    )
    # LTP (24988.0) hasn't reached its downside target (24990.0) yet -> negative signed
    # distance, and "bounce" (not "pullback") is the reversal word on this side.
    below_target = signals._build_tags(
        ltp=24988.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=None,
        ema9_15m_position=None,
        atr14_5m=None,
        opening_range_high=25100.0,
        opening_range_low=25000.0,
        opening_range_position="below",
        nearest_level=None,
        nearest_or_target={"label": "OR Target 1", "value": 24990.0, "distance_percent": 0.01},
        pcr=None,
        pcr_bias=None,
        max_pain=None,
        max_pain_pull=None,
        oi_support_strike=None,
        oi_resistance_strike=None,
    )

    assert on_target == ["Above opening range by 10.00 (near OR Target 1 by +0.00, caution: possible pullback)"]
    assert past_target == ["Above opening range by 12.00 (near OR Target 1 by +2.00, caution: possible pullback)"]
    assert below_target == ["Below opening range by 12.00 (near OR Target 1 by -2.00, caution: possible bounce)"]


def test_build_tags_adds_pcr_and_max_pain_tags() -> None:
    tags = signals._build_tags(
        ltp=25050.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=None,
        ema9_15m_position=None,
        atr14_5m=None,
        opening_range_high=None,
        opening_range_low=None,
        opening_range_position=None,
        nearest_level=None,
        nearest_or_target=None,
        pcr=1.35,
        pcr_bias="bullish",
        max_pain=25000.0,
        max_pain_pull="bearish",
        oi_support_strike=None,
        oi_resistance_strike=None,
    )

    assert tags == [
        "PCR 1.35 - Bullish bias",
        "Max Pain 25000 by +50.00 - Bearish pull",
    ]


def test_build_tags_omits_pcr_and_max_pain_when_unavailable() -> None:
    tags = signals._build_tags(
        ltp=25050.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=None,
        ema9_15m_position=None,
        atr14_5m=None,
        opening_range_high=None,
        opening_range_low=None,
        opening_range_position=None,
        nearest_level=None,
        nearest_or_target=None,
        pcr=None,
        pcr_bias=None,
        max_pain=None,
        max_pain_pull=None,
        oi_support_strike=None,
        oi_resistance_strike=None,
    )

    assert tags == []


def test_build_tags_adds_oi_support_and_resistance_tags() -> None:
    tags = signals._build_tags(
        ltp=25050.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=None,
        ema9_15m_position=None,
        atr14_5m=None,
        opening_range_high=None,
        opening_range_low=None,
        opening_range_position=None,
        nearest_level=None,
        nearest_or_target=None,
        pcr=None,
        pcr_bias=None,
        max_pain=None,
        max_pain_pull=None,
        oi_support_strike=24900.0,
        oi_resistance_strike=25200.0,
    )

    assert tags == [
        "OI Support 24900 by +150.00",
        "OI Resistance 25200 by -150.00",
    ]


def test_oi_support_resistance_picks_highest_put_and_call_oi_strikes() -> None:
    rows = [
        {"strike_price": 24900.0, "call_oi": 500000, "put_oi": 1200000},
        {"strike_price": 25000.0, "call_oi": 800000, "put_oi": 900000},
        {"strike_price": 25100.0, "call_oi": 1500000, "put_oi": 400000},
    ]

    support_strike, support_oi, resistance_strike, resistance_oi = signals._oi_support_resistance(rows)

    assert (support_strike, support_oi) == (24900.0, 1200000.0)
    assert (resistance_strike, resistance_oi) == (25100.0, 1500000.0)


def test_oi_support_resistance_returns_none_for_empty_or_unusable_rows() -> None:
    assert signals._oi_support_resistance([]) == (None, None, None, None)
    assert signals._oi_support_resistance([{"strike_price": None, "call_oi": 100, "put_oi": 100}]) == (
        None, None, None, None,
    )


def test_or_targets_are_ordinal_multiples_of_the_or_size() -> None:
    up_targets = signals._or_targets(25100.0, 100.0, sign=1)
    down_targets = signals._or_targets(25000.0, 100.0, sign=-1)

    assert up_targets == {
        "OR Target 1": 25150.0,
        "OR Target 2": 25200.0,
        "OR Target 3": 25250.0,
        "OR Target 4": 25300.0,
    }
    assert down_targets == {
        "OR Target 1": 24950.0,
        "OR Target 2": 24900.0,
        "OR Target 3": 24850.0,
        "OR Target 4": 24800.0,
    }


def test_nearest_or_target_only_considers_the_breakout_side() -> None:
    # LTP sitting right on the upside Target 1 (25150.0) -- should match when the breakout is
    # "above", but never even be considered when it's "below" (wrong side) or "inside" (no
    # breakout at all).
    above = signals._nearest_or_target(
        25150.0, opening_range_high=25100.0, opening_range_low=25000.0,
        opening_range_position="above", tolerance_percent=0.15,
    )
    below = signals._nearest_or_target(
        25150.0, opening_range_high=25100.0, opening_range_low=25000.0,
        opening_range_position="below", tolerance_percent=0.15,
    )
    inside = signals._nearest_or_target(
        25150.0, opening_range_high=25100.0, opening_range_low=25000.0,
        opening_range_position="inside", tolerance_percent=0.15,
    )

    assert above == {"label": "OR Target 1", "value": 25150.0, "distance_percent": 0.0}
    assert below is None
    assert inside is None


def test_nearest_or_target_returns_none_without_a_valid_opening_range() -> None:
    assert signals._nearest_or_target(
        25150.0, opening_range_high=None, opening_range_low=25000.0,
        opening_range_position="above", tolerance_percent=0.15,
    ) is None
    # A zero/negative-size OR (shouldn't happen in practice, but guard it) has no meaningful
    # measured-move targets to compute.
    assert signals._nearest_or_target(
        25150.0, opening_range_high=25000.0, opening_range_low=25000.0,
        opening_range_position="above", tolerance_percent=0.15,
    ) is None


# --- end-to-end wiring via a fake UpstoxService double ------------------------------------


class _FakeUpstoxService:
    """Duck-typed stand-in for UpstoxService -- UnderlyingSignalsService only calls these
    methods, so a full HTTP-mocked UpstoxService isn't needed to test the wiring. The four OI
    methods (get_oi/get_change_oi/get_max_pain/get_pcr) are only ever exercised when a test
    passes expiry_date -- get_signals skips OIAnalysisService entirely otherwise.
    """

    def __init__(
        self,
        *,
        minute_candles: list[list[object]],
        daily_candles: list[list[object]],
        strikes: list[float],
        ltp: float,
        pcr: float = 1.0,
        max_pain: float = 0.0,
        oi_strike_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._minute_candles = minute_candles
        self._daily_candles = daily_candles
        self._strikes = strikes
        self._ltp = ltp
        self._pcr = pcr
        self._max_pain = max_pain
        self._oi_strike_rows = oi_strike_rows or []

    async def get_historical_candle(self, access_token, instrument_key, *, unit, interval, to_date, from_date=None):
        candles = self._daily_candles if unit == "days" else self._minute_candles
        return {"status": "success", "data": {"candles": candles}}

    async def get_intraday_candle(self, access_token, instrument_key, *, unit, interval):
        return {"status": "success", "data": {"candles": []}}

    async def get_option_contracts(self, access_token, instrument_key, *, expiry_date=None):
        return {"status": "success", "data": [{"strike_price": strike} for strike in self._strikes]}

    async def get_quotes(self, access_token, instrument_key):
        return {"status": "success", "data": {instrument_key: {"last_price": self._ltp}}}

    async def get_oi(self, access_token, instrument_key, *, expiry, date):
        return {
            "status": "success",
            "data": {"total_puts": 0, "total_calls": 0, "call_put_oi_data_list": self._oi_strike_rows},
        }

    async def get_change_oi(self, access_token, instrument_key, *, expiry, date, interval):
        return {"status": "success", "data": {"call_put_oi_data_list": []}}

    async def get_max_pain(self, access_token, instrument_key, *, expiry, date, bucket_interval):
        return {"status": "success", "data": {"max_pain": self._max_pain, "insights": []}}

    async def get_pcr(self, access_token, instrument_key, *, expiry, date, bucket_interval):
        return {"status": "success", "data": {"pcr": self._pcr, "insights": []}}


def _rising_candles(count: int, *, start: datetime, step_minutes: int) -> list[list[object]]:
    rows: list[list[object]] = []
    price = 100.0
    for i in range(count):
        ts = (start + timedelta(minutes=step_minutes * i)).isoformat()
        high = price + 2.0
        low = price - 2.0
        close = price + 1.0
        rows.append([ts, price, high, low, close, 1000])
        price += 1.0
    return rows


def test_get_signals_wires_everything_into_tags() -> None:
    """A rising intraday series, an uptrending daily candle, and an LTP above everything should
    read as bullish across the board -- this is a wiring/shape check, the exact math is already
    covered by the focused unit tests above.
    """
    start = datetime(2026, 7, 18, 9, 15)
    minute_candles = _rising_candles(20, start=start, step_minutes=5)
    daily_candles = [
        ["2026-07-17T00:00:00+05:30", 100.0, 110.0, 95.0, 105.0, 500000],
    ]
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=minute_candles,
            daily_candles=daily_candles,
            strikes=[24800.0, 24850.0, 24900.0, 24950.0],
            ltp=200.0,
        ),
    )

    result = anyio.run(
        lambda: service.get_signals("upstox-token", underlying_key="NSE_INDEX|Nifty 50"),
    )

    assert result["ltp"] == 200.0
    assert result["ema9_5m"]["position"] == "above"
    assert result["ema9_15m"]["position"] == "above"
    assert result["atr14_5m"] is not None
    assert result["opening_range"]["position"] == "above"
    assert result["previous_day"] == {"high": 110.0, "low": 95.0, "close": 105.0}
    assert result["round_step"] == 50.0
    # Prefix checks (not exact-match) since every directional tag now also spells out the
    # absolute point distance -- see _build_tags -- which this wiring test isn't pinning down.
    assert any(tag.startswith("Above 5m EMA9 by ") for tag in result["tags"])
    assert any(tag.startswith("Above 15m EMA9 by ") for tag in result["tags"])
    assert any(tag.startswith("Above opening range by ") for tag in result["tags"])
    # No expiry_date was passed -- OI analysis is skipped entirely, not just empty.
    assert result["pcr"] is None
    assert result["max_pain"] is None
    assert result["oi_support"] is None
    assert result["oi_resistance"] is None


def test_get_signals_includes_pcr_and_max_pain_when_expiry_date_is_given() -> None:
    start = datetime(2026, 7, 18, 9, 15)
    minute_candles = _rising_candles(20, start=start, step_minutes=5)
    daily_candles = [
        ["2026-07-17T00:00:00+05:30", 100.0, 110.0, 95.0, 105.0, 500000],
    ]
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=minute_candles,
            daily_candles=daily_candles,
            strikes=[24800.0, 24850.0, 24900.0, 24950.0],
            ltp=200.0,
            pcr=1.35,
            max_pain=190.0,
            oi_strike_rows=[
                {"strike_price": 190.0, "call_oi": 500000, "put_oi": 1200000},
                {"strike_price": 210.0, "call_oi": 1500000, "put_oi": 400000},
            ],
        ),
    )

    result = anyio.run(
        lambda: service.get_signals(
            "upstox-token", underlying_key="NSE_INDEX|Nifty 50", expiry_date="2026-07-23",
        ),
    )

    assert result["pcr"] == {"value": 1.35, "bias": "bullish"}
    # LTP (200.0) is above max pain (190.0) -> bearish pull.
    assert result["max_pain"] == {"value": 190.0, "pull": "bearish"}
    assert result["oi_support"] == {"value": 190.0, "oi": 1200000.0}
    assert result["oi_resistance"] == {"value": 210.0, "oi": 1500000.0}
    assert any(tag.startswith("PCR 1.35 - Bullish bias") for tag in result["tags"])
    assert any(tag.startswith("Max Pain 190 by +10.00 - Bearish pull") for tag in result["tags"])
    assert any(tag.startswith("OI Support 190 by +10.00") for tag in result["tags"])
    assert any(tag.startswith("OI Resistance 210 by -10.00") for tag in result["tags"])

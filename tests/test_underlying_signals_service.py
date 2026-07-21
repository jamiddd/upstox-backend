from __future__ import annotations

from datetime import datetime, timedelta

import anyio
import pytest

from app.services import underlying_signals_service as signals
from app.services.underlying_signals_service import Candle, UnderlyingSignalsService


@pytest.fixture(autouse=True)
def clear_signals_cache() -> None:
    signals._CACHE = {}
    signals._HISTORY = {}


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


def test_is_near_day_open_flags_ltp_within_the_fixed_point_tolerance() -> None:
    assert signals._is_near_day_open(25010.0, 25000.0, tolerance_points=15.0) is True
    assert signals._is_near_day_open(24985.0, 25000.0, tolerance_points=15.0) is True
    # Exactly at the boundary is still "near" (inclusive).
    assert signals._is_near_day_open(25015.0, 25000.0, tolerance_points=15.0) is True
    assert signals._is_near_day_open(25016.0, 25000.0, tolerance_points=15.0) is False
    assert signals._is_near_day_open(25000.0, None, tolerance_points=15.0) is False
    assert signals._is_near_day_open(0.0, 25000.0, tolerance_points=15.0) is False


def _record(
    key,
    *,
    now: float,
    atr=None,
    vwap_distance=None,
    level_distance=None,
    pcr=None,
    support_strike=None,
    support_oi=None,
    support_call_oi=None,
    resistance_strike=None,
    resistance_oi=None,
    resistance_put_oi=None,
    atm_straddle=None,
):
    """Thin wrapper around _record_and_diff with every optional metric defaulted to None, so each
    test only has to spell out the ones it actually cares about."""
    return signals._record_and_diff(
        key,
        atr=atr,
        vwap_distance=vwap_distance,
        level_distance=level_distance,
        pcr=pcr,
        support_strike=support_strike,
        support_oi=support_oi,
        support_call_oi=support_call_oi,
        resistance_strike=resistance_strike,
        resistance_oi=resistance_oi,
        resistance_put_oi=resistance_put_oi,
        atm_straddle=atm_straddle,
        now=now,
    )


def test_record_and_diff_returns_no_deltas_with_no_prior_history() -> None:
    deltas = _record(("NSE_INDEX|Nifty 50", None), now=1000.0, atr=20.0, pcr=1.1)

    assert deltas.atr is None
    assert deltas.pcr is None


def test_record_and_diff_computes_exact_delta_for_a_five_minute_old_snapshot() -> None:
    key = ("NSE_INDEX|Nifty 50", None)
    _record(key, now=1000.0, atr=20.0, vwap_distance=10.0, level_distance=5.0, pcr=1.1, atm_straddle=250.0)

    deltas = _record(key, now=1000.0 + 300.0, atr=23.5, vwap_distance=6.0, level_distance=8.0, pcr=1.3, atm_straddle=260.0)

    assert deltas.atr == pytest.approx(3.5)
    # Negative -- the distance shrank (price closing in), not VWAP's own value.
    assert deltas.vwap_distance == pytest.approx(-4.0)
    assert deltas.level_distance == pytest.approx(3.0)
    assert deltas.pcr == pytest.approx(0.2)
    assert deltas.atm_straddle == pytest.approx(10.0)


def test_record_and_diff_ignores_a_snapshot_outside_the_five_minute_band() -> None:
    key = ("NSE_INDEX|Nifty 50", None)
    _record(key, now=1000.0, atr=20.0)

    # Only 3 minutes old -- outside the 4-6 minute tolerance band.
    deltas = _record(key, now=1000.0 + 180.0, atr=25.0)

    assert deltas.atr is None


def test_record_and_diff_prunes_snapshots_older_than_the_history_window() -> None:
    key = ("NSE_INDEX|Nifty 50", None)
    _record(key, now=1000.0, atr=20.0)

    # 11 minutes later -- past the 10-minute prune window, so the first snapshot is gone by the
    # time this third call looks 5 minutes back from it.
    _record(key, now=1000.0 + 660.0, atr=30.0)
    deltas = _record(key, now=1000.0 + 660.0 + 300.0, atr=35.0)

    assert deltas.atr == pytest.approx(5.0)  # only ever compared against the 30.0 snapshot
    assert len(signals._HISTORY[key]) == 2  # the original 20.0 snapshot was pruned out


def test_record_and_diff_one_metric_missing_does_not_affect_the_others() -> None:
    key = ("NSE_INDEX|Nifty 50", None)
    _record(key, now=1000.0, atr=20.0, pcr=None)

    deltas = _record(key, now=1000.0 + 300.0, atr=22.0, pcr=1.4)

    assert deltas.atr == pytest.approx(2.0)
    assert deltas.pcr is None  # no PCR 5 minutes ago to compare against


def test_record_and_diff_oi_support_resistance_require_the_same_strike() -> None:
    key = ("NSE_INDEX|Nifty 50", "2026-07-23")
    _record(
        key, now=1000.0,
        support_strike=24900.0, support_oi=1_000_000.0, support_call_oi=300_000.0,
        resistance_strike=25200.0, resistance_oi=800_000.0, resistance_put_oi=200_000.0,
    )

    # Support strike unchanged -> real deltas on both sides. Resistance strike moved to a
    # different strike -> no apples-to-apples comparison, so both its deltas stay None even
    # though all the OI numbers exist.
    deltas = _record(
        key, now=1000.0 + 300.0,
        support_strike=24900.0, support_oi=1_200_000.0, support_call_oi=350_000.0,
        resistance_strike=25300.0, resistance_oi=900_000.0, resistance_put_oi=250_000.0,
    )

    assert deltas.support_oi == pytest.approx(200_000.0)
    assert deltas.support_call_oi == pytest.approx(50_000.0)
    assert deltas.resistance_oi is None
    assert deltas.resistance_put_oi is None


def test_record_and_diff_atm_straddle_has_no_strike_gating() -> None:
    """Unlike OI support/resistance, ATM straddle is diffed unconditionally -- the ATM strike is
    expected to roll as price moves, so there's no strike-matching requirement to satisfy."""
    key = ("NSE_INDEX|Nifty 50", "2026-07-23")
    _record(key, now=1000.0, atm_straddle=250.0)

    deltas = _record(key, now=1000.0 + 300.0, atm_straddle=310.0)

    assert deltas.atm_straddle == pytest.approx(60.0)


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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
    )

    assert tags == [
        "Above 5m EMA9 by 50.00 (15m Below by 50.00)",
        "ATR 42.3",
        "Inside opening range",
        "Near R1 Pivot by 13.40",
    ]


def test_build_tags_merges_5m_and_15m_ema_into_one_line_when_both_agree() -> None:
    tags = signals._build_tags(
        ltp=25100.0,
        ema9_5m_value=25050.0,
        ema9_5m_position="above",
        ema9_15m_value=25000.0,
        ema9_15m_position="above",
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
    )

    assert tags == ["Above 5m EMA9 by 50.00 (15m Above by 100.00)"]


def test_build_tags_shows_5m_or_15m_ema_alone_when_only_one_is_available() -> None:
    only_5m = signals._build_tags(
        ltp=25100.0,
        ema9_5m_value=25050.0,
        ema9_5m_position="above",
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
    )
    only_15m = signals._build_tags(
        ltp=25100.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=25000.0,
        ema9_15m_position="above",
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
    )

    assert only_5m == ["Above 5m EMA9 by 50.00"]
    assert only_15m == ["Above 15m EMA9 by 100.00"]


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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
    )

    assert tags == [
        "PCR 1.35",
        "MP 25000 (+50.0)",
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
    )

    assert tags == [
        "OI(S) 24900",
        "OI(R) 25200",
    ]


def test_build_tags_adds_vwap_tag() -> None:
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
        vwap_value=25000.0,
        vwap_position="above",
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
    )

    assert tags == ["Above VWAP by 50.00"]


def test_short_oi_delta_formats_every_magnitude_tier() -> None:
    assert signals._short_oi_delta(None) is None
    # Below a full lakh -- two decimals, so a sub-lakh change still carries useful precision.
    assert signals._short_oi_delta(81_000.0) == "+0.81L"
    assert signals._short_oi_delta(-50_000.0) == "-0.50L"
    assert signals._short_oi_delta(500.0) == "+0.01L"
    # At/above a full lakh -- one decimal.
    assert signals._short_oi_delta(410_000.0) == "+4.1L"
    assert signals._short_oi_delta(-110_000.0) == "-1.1L"
    # At/above a full crore.
    assert signals._short_oi_delta(1_20_00_000.0) == "+1.2Cr"


def test_build_tags_appends_five_minute_change_suffixes_when_present() -> None:
    tags = signals._build_tags(
        ltp=25050.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=None,
        ema9_15m_position=None,
        atr14_5m=42.349,
        opening_range_high=None,
        opening_range_low=None,
        opening_range_position=None,
        nearest_level={"label": "R1 Pivot", "value": 25040.0, "distance_percent": 0.04},
        nearest_or_target=None,
        pcr=1.35,
        pcr_bias="bullish",
        max_pain=None,
        max_pain_pull=None,
        oi_support_strike=24900.0,
        oi_resistance_strike=25200.0,
        vwap_value=25000.0,
        vwap_position="above",
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=245.60,
        atr_delta=2.1,
        vwap_distance_delta=-4.0,
        level_distance_delta=3.0,
        pcr_delta=-0.15,
        support_oi_delta=120000.0,
        support_call_oi_delta=410000.0,
        resistance_oi_delta=-50000.0,
        resistance_put_oi_delta=-110000.0,
        atm_straddle_delta=12.3,
    )

    assert tags == [
        "ATR 42.3 (+2.10 in 5m)",
        "Near R1 Pivot by 10.00 (+3.00 in 5m)",
        "PCR 1.35 (-0.15 in 5m)",
        "OI(S) 24900 (C/+4.1L, P/+1.2L)",
        "OI(R) 25200 (C/-0.50L, P/-1.1L)",
        "Above VWAP by 50.00 (-4.00 in 5m)",
        "STR(ATM) 245.6 (+12.3)",
    ]


def test_build_tags_omits_five_minute_change_suffixes_when_absent() -> None:
    tags = signals._build_tags(
        ltp=25050.0,
        ema9_5m_value=None,
        ema9_5m_position=None,
        ema9_15m_value=None,
        ema9_15m_position=None,
        atr14_5m=42.349,
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
        vwap_value=None,
        vwap_position=None,
        today_open=None,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
    )

    assert tags == ["ATR 42.3"]  # no "(... in 5m)" suffix, and no ATM Straddle tag at all


def test_build_tags_puts_no_trade_zone_caution_first_ahead_of_every_other_tag() -> None:
    tags = signals._build_tags(
        ltp=25010.0,
        ema9_5m_value=25000.0,
        ema9_5m_position="above",
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
        vwap_value=None,
        vwap_position=None,
        today_open=25000.0,
        no_trade_zone=True,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
    )

    assert tags == [
        "No-Trade Zone -- within 15 of Day Open (25000)",
        "Above 5m EMA9 by 10.00",
    ]


def test_build_tags_omits_no_trade_zone_caution_when_not_flagged() -> None:
    tags = signals._build_tags(
        ltp=25010.0,
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
        vwap_value=None,
        vwap_position=None,
        today_open=25000.0,
        no_trade_zone=False,
        no_trade_zone_points=15.0,
        atm_straddle=None,
        atr_delta=None,
        vwap_distance_delta=None,
        level_distance_delta=None,
        pcr_delta=None,
        support_oi_delta=None,
        support_call_oi_delta=None,
        resistance_oi_delta=None,
        resistance_put_oi_delta=None,
        atm_straddle_delta=None,
    )

    assert tags == []


def test_oi_support_resistance_picks_highest_put_and_call_oi_strikes() -> None:
    rows = [
        {"strike_price": 24900.0, "call_oi": 500000, "put_oi": 1200000},
        {"strike_price": 25000.0, "call_oi": 800000, "put_oi": 900000},
        {"strike_price": 25100.0, "call_oi": 1500000, "put_oi": 400000},
    ]

    (
        support_strike, support_oi, support_call_oi,
        resistance_strike, resistance_oi, resistance_put_oi,
    ) = signals._oi_support_resistance(rows)

    assert (support_strike, support_oi) == (24900.0, 1200000.0)
    assert (resistance_strike, resistance_oi) == (25100.0, 1500000.0)
    # The *other* side's OI at each of those same two strikes -- call OI at 24900 (the support
    # strike), put OI at 25100 (the resistance strike).
    assert support_call_oi == 500000.0
    assert resistance_put_oi == 400000.0


def test_oi_support_resistance_returns_none_for_empty_or_unusable_rows() -> None:
    assert signals._oi_support_resistance([]) == (None, None, None, None, None, None)
    assert signals._oi_support_resistance([{"strike_price": None, "call_oi": 100, "put_oi": 100}]) == (
        None, None, None, None, None, None,
    )


def test_near_atm_strikes_excludes_rows_beyond_count_on_either_side() -> None:
    # Strikes 170..230 step 5 (13 rows), ATM (nearest to ltp=200.0) at index 6 -> with count=5
    # the window is indices [1:12], i.e. strikes 175..225 -- excludes 170 and 230.
    rows = [{"strike_price": float(s)} for s in range(170, 231, 5)]

    near = signals._near_atm_strikes(rows, 200.0, count=5)

    assert [row["strike_price"] for row in near] == [float(s) for s in range(175, 226, 5)]


def test_near_atm_strikes_clamps_at_the_edges_of_the_chain() -> None:
    # ATM sits right at the first listed strike -- there's nothing below it to include, so the
    # window just clamps to what's actually there instead of erroring.
    rows = [{"strike_price": float(s)} for s in range(200, 231, 5)]

    near = signals._near_atm_strikes(rows, 200.0, count=5)

    assert [row["strike_price"] for row in near] == [float(s) for s in range(200, 226, 5)]


def test_near_atm_strikes_returns_empty_for_no_usable_rows() -> None:
    assert signals._near_atm_strikes([], 200.0, count=5) == []
    assert signals._near_atm_strikes([{"strike_price": None}], 200.0, count=5) == []


def test_local_pcr_sums_put_and_call_oi_across_the_given_rows() -> None:
    rows = [
        {"strike_price": 190.0, "call_oi": 500000, "put_oi": 1200000},
        {"strike_price": 210.0, "call_oi": 1500000, "put_oi": 400000},
    ]

    pcr = signals._local_pcr(rows)

    assert pcr == pytest.approx(1600000 / 2000000)


def test_local_pcr_returns_none_when_there_is_no_call_oi_to_divide_by() -> None:
    assert signals._local_pcr([]) is None
    assert signals._local_pcr([{"strike_price": 200.0, "call_oi": 0, "put_oi": 500}]) is None


def test_vwap_is_typical_price_weighted_by_volume_for_todays_candles_only() -> None:
    candles = [
        Candle(timestamp="2026-07-17T09:15:00+05:30", open=0.0, high=100.0, low=100.0, close=100.0, volume=99999.0),
        Candle(timestamp="2026-07-18T09:15:00+05:30", open=99.0, high=102.0, low=98.0, close=100.0, volume=100.0),
        Candle(timestamp="2026-07-18T09:20:00+05:30", open=100.0, high=104.0, low=100.0, close=102.0, volume=300.0),
    ]

    vwap = signals._vwap(candles)

    # typical prices: (102+98+100)/3=100.0, (104+100+102)/3=102.0 -- weighted by 100/300 volume.
    assert vwap == pytest.approx((100.0 * 100.0 + 102.0 * 300.0) / 400.0)


def test_vwap_returns_none_on_zero_volume() -> None:
    candles = [
        Candle(timestamp="2026-07-18T09:15:00+05:30", open=100.0, high=101.0, low=99.0, close=100.0, volume=0.0),
    ]

    assert signals._vwap(candles) is None
    assert signals._vwap([]) is None


def test_is_sensex_matches_the_expected_key_or_a_symbol_text_fallback() -> None:
    assert signals._is_sensex("BSE_INDEX|SENSEX", None) is True
    assert signals._is_sensex("BSE_INDEX|Some Other Key", "sensex") is True
    assert signals._is_sensex("BSE_INDEX|Some Other Key", "  Sensex  ") is True
    assert signals._is_sensex("NSE_INDEX|Nifty 50", "NIFTY") is False
    assert signals._is_sensex("NSE_INDEX|Nifty 50", None) is False


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
        futures_search_rows: list[dict[str, object]] | None = None,
        futures_candles: list[list[object]] | None = None,
        futures_ltp: float = 0.0,
        option_chain_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._minute_candles = minute_candles
        self._daily_candles = daily_candles
        self._strikes = strikes
        self._ltp = ltp
        self._pcr = pcr
        self._max_pain = max_pain
        self._oi_strike_rows = oi_strike_rows or []
        self._futures_search_rows = futures_search_rows or []
        self._futures_candles = futures_candles or []
        self._futures_ltp = futures_ltp
        self._option_chain_rows = option_chain_rows or []

    async def get_historical_candle(self, access_token, instrument_key, *, unit, interval, to_date, from_date=None):
        if instrument_key == "NSE_FO|53216":
            candles = self._futures_candles if unit != "days" else []
        else:
            candles = self._daily_candles if unit == "days" else self._minute_candles
        return {"status": "success", "data": {"candles": candles}}

    async def get_intraday_candle(self, access_token, instrument_key, *, unit, interval):
        return {"status": "success", "data": {"candles": []}}

    async def get_option_contracts(self, access_token, instrument_key, *, expiry_date=None):
        return {"status": "success", "data": [{"strike_price": strike} for strike in self._strikes]}

    async def get_quotes(self, access_token, instrument_key):
        ltp = self._futures_ltp if instrument_key == "NSE_FO|53216" else self._ltp
        return {"status": "success", "data": {instrument_key: {"last_price": ltp}}}

    async def search_instruments(self, access_token, *, query, segments, instrument_types, expiry, records):
        return {"status": "success", "data": self._futures_search_rows}

    async def get_option_chain(self, access_token, instrument_key, *, expiry_date):
        return {"status": "success", "data": self._option_chain_rows}

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
    # absolute point distance -- see _build_tags -- which this wiring test isn't pinning down. The
    # 5m and 15m EMA reads are folded into a single line (both "above" here), with the 15m read
    # parenthesized alongside the 5m one -- see _build_tags's merge doc comment.
    assert any(tag.startswith("Above 5m EMA9 by ") and "(15m Above by " in tag for tag in result["tags"])
    assert any(tag.startswith("Above opening range by ") for tag in result["tags"])
    # No expiry_date was passed -- OI analysis is skipped entirely, not just empty.
    assert result["pcr"] is None
    assert result["max_pain"] is None
    assert result["oi_support"] is None
    assert result["oi_resistance"] is None
    # No underlying_symbol was passed -- futures resolution (and therefore VWAP) is skipped
    # entirely, not just empty.
    assert result["vwap"] is None
    # Today's session open (the fake series' first candle) is 100.0, LTP is 200.0 -- nowhere near
    # it, so no caution.
    assert result["today_open"] == 100.0
    assert result["no_trade_zone"] is False


def test_get_signals_flags_no_trade_zone_when_ltp_is_near_todays_open() -> None:
    """LTP sitting just 3 points above today's own session open should flag the no-trade-zone
    caution and put it first in the tag list. The no-trade-zone tolerance is now ATR-scaled (see
    _NO_TRADE_ZONE_ATR_MULTIPLIER/_NO_TRADE_ZONE_MIN_POINTS) rather than a flat 15 points -- the
    `_rising_candles` fixture's constant 4-point true range converges ATR(14) to exactly 4.0, so
    `4.0 * 0.75 = 3.0` is below the 5.0-point floor, meaning the *floor* is what actually applies
    here (5.0), not the ATR-scaled value -- 3 points away is comfortably inside that.
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
            ltp=103.0,
        ),
    )

    result = anyio.run(
        lambda: service.get_signals("upstox-token", underlying_key="NSE_INDEX|Nifty 50"),
    )

    assert result["today_open"] == 100.0
    assert result["no_trade_zone"] is True
    assert result["tags"][0] == "No-Trade Zone -- within 5 of Day Open (100)"


def test_get_signals_no_trade_zone_tolerance_scales_with_atr() -> None:
    """A higher-ATR session gets a wider no-trade-zone tolerance than the 5.0-point floor -- LTP
    16 points from today's open is outside the fixed old 15-point constant but should still flag
    once ATR is high enough to scale the dynamic tolerance past it.
    """
    start = datetime(2026, 7, 18, 9, 15)
    # A wider, still-constant true range (20, not 4) so ATR(14) converges to 20.0 -- scaled
    # tolerance = 20.0 * 0.75 = 15.0, still not quite 16, so push the true range a bit further.
    minute_candles = []
    price = 100.0
    for i in range(20):
        ts = (start + timedelta(minutes=5 * i)).isoformat()
        minute_candles.append([ts, price, price + 12.0, price - 12.0, price + 1.0, 1000])
        price += 1.0
    daily_candles = [
        ["2026-07-17T00:00:00+05:30", 100.0, 110.0, 95.0, 105.0, 500000],
    ]
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=minute_candles,
            daily_candles=daily_candles,
            strikes=[24800.0, 24850.0, 24900.0, 24950.0],
            ltp=116.0,
        ),
    )

    result = anyio.run(
        lambda: service.get_signals("upstox-token", underlying_key="NSE_INDEX|Nifty 50"),
    )

    # True range is a flat 24 every candle (high-low) -- ATR(14) converges to 24.0, so the
    # ATR-scaled tolerance is 24.0 * 0.75 = 18.0, comfortably past the 16-point gap from open.
    assert result["today_open"] == 100.0
    assert result["no_trade_zone"] is True
    assert "No-Trade Zone -- within 18 of Day Open (100)" in result["tags"]


def test_get_signals_includes_pcr_and_max_pain_when_expiry_date_is_given() -> None:
    start = datetime(2026, 7, 18, 9, 15)
    minute_candles = _rising_candles(20, start=start, step_minutes=5)
    daily_candles = [
        ["2026-07-17T00:00:00+05:30", 100.0, 110.0, 95.0, 105.0, 500000],
    ]
    # 13 strikes, 170..230 step 5, ATM (nearest to ltp=200.0) at index 6 -- with
    # _NEAR_ATM_STRIKE_COUNT=5, the near-ATM window is indices [1:12], i.e. 175..225, excluding
    # the two extremes (170 and 230). Both extremes carry a deliberately huge, otherwise-winning
    # OI print to prove they get excluded rather than dominating PCR/support/resistance.
    oi_strike_rows = [
        {"strike_price": 170.0, "call_oi": 100, "put_oi": 99999999},  # excluded
        {"strike_price": 175.0, "call_oi": 0, "put_oi": 0},
        {"strike_price": 180.0, "call_oi": 0, "put_oi": 0},
        {"strike_price": 185.0, "call_oi": 0, "put_oi": 0},
        {"strike_price": 190.0, "call_oi": 0, "put_oi": 1200000},  # support winner (in window)
        {"strike_price": 195.0, "call_oi": 0, "put_oi": 0},
        {"strike_price": 200.0, "call_oi": 0, "put_oi": 0},
        {"strike_price": 205.0, "call_oi": 0, "put_oi": 0},
        {"strike_price": 210.0, "call_oi": 1500000, "put_oi": 0},  # resistance winner (in window)
        {"strike_price": 215.0, "call_oi": 0, "put_oi": 0},
        {"strike_price": 220.0, "call_oi": 0, "put_oi": 0},
        {"strike_price": 225.0, "call_oi": 0, "put_oi": 0},
        {"strike_price": 230.0, "call_oi": 99999999, "put_oi": 100},  # excluded
    ]
    class _SnapshotStore:
        calls = []

        def record_and_find_previous(self, **kwargs):
            self.calls.append(kwargs)
            return None

    snapshot_store = _SnapshotStore()
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=minute_candles,
            daily_candles=daily_candles,
            strikes=[24800.0, 24850.0, 24900.0, 24950.0],
            ltp=200.0,
            max_pain=190.0,
            oi_strike_rows=oi_strike_rows,
        ),
        snapshot_store=snapshot_store,
    )

    result = anyio.run(
        lambda: service.get_signals(
            "upstox-token", underlying_key="NSE_INDEX|Nifty 50", expiry_date="2026-07-23",
        ),
    )

    # Local PCR over just the near-ATM window: 1200000 put / 1500000 call = 0.8 -> bearish. If the
    # excluded strikes had leaked in, this would instead be ~1.0 (huge put and huge call roughly
    # cancel out) or skewed the other way -- either way, not 0.8.
    assert result["pcr"] == {"value": 0.8, "bias": "bearish"}
    assert len(snapshot_store.calls) == 1
    assert snapshot_store.calls[0]["expiry_date"] == "2026-07-23"
    assert snapshot_store.calls[0]["metrics"]["pcr"] == pytest.approx(0.8)
    # LTP (200.0) is above max pain (190.0) -> bearish pull. max_pain itself is intentionally
    # NOT restricted to the near-ATM window (see _oi_analysis's doc comment).
    assert result["max_pain"] == {"value": 190.0, "pull": "bearish"}
    assert result["oi_support"] == {"value": 190.0, "oi": 1200000.0}
    assert result["oi_resistance"] == {"value": 210.0, "oi": 1500000.0}
    assert any(tag.startswith("PCR 0.80") for tag in result["tags"])
    assert any(tag.startswith("MP 190 (+10.0)") for tag in result["tags"])
    assert any(tag.startswith("OI(S) 190") for tag in result["tags"])
    assert any(tag.startswith("OI(R) 210") for tag in result["tags"])


# --- futures resolution + VWAP wiring --------------------------------------------------------


_NIFTY_FUT_ROW = {
    "name": "Nifty Future",
    "exchange": "NSE",
    "instrument_type": "FUT",
    "instrument_key": "NSE_FO|53216",
    "trading_symbol": "NIFTY FUT 31 JUL 26",
    "underlying_key": "NSE_INDEX|Nifty 50",
    "underlying_type": "INDEX",
    "underlying_symbol": "NIFTY",
    "lot_size": 75,
    "freeze_quantity": 1800.0,
    "tick_size": 5.0,
}


def test_futures_instrument_key_matches_exact_underlying_key() -> None:
    # A same-symbol-text row for a *different* underlying_key (e.g. FINNIFTY's own future also
    # matching a "NIFTY" text search) must lose to the exact underlying_key match.
    decoy_row = {**_NIFTY_FUT_ROW, "instrument_key": "NSE_FO|99999", "underlying_key": "NSE_INDEX|Fin Nifty"}
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=[], daily_candles=[], strikes=[], ltp=0.0,
            futures_search_rows=[decoy_row, _NIFTY_FUT_ROW],
        ),
    )

    resolved = anyio.run(
        lambda: service._futures_instrument_key(
            "upstox-token", underlying_key="NSE_INDEX|Nifty 50", underlying_symbol="NIFTY",
        ),
    )

    assert resolved == "NSE_FO|53216"


def test_futures_instrument_key_returns_none_without_underlying_symbol() -> None:
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=[], daily_candles=[], strikes=[], ltp=0.0,
            futures_search_rows=[_NIFTY_FUT_ROW],
        ),
    )

    resolved = anyio.run(
        lambda: service._futures_instrument_key(
            "upstox-token", underlying_key="NSE_INDEX|Nifty 50", underlying_symbol=None,
        ),
    )

    assert resolved is None


def test_futures_instrument_key_returns_none_when_no_future_is_listed() -> None:
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=[], daily_candles=[], strikes=[], ltp=0.0,
            futures_search_rows=[],
        ),
    )

    resolved = anyio.run(
        lambda: service._futures_instrument_key(
            "upstox-token", underlying_key="NSE_EQ|INE002A01018", underlying_symbol="RELIANCE",
        ),
    )

    assert resolved is None


def test_futures_instrument_key_resolves_sensex_to_niftys_own_future() -> None:
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=[], daily_candles=[], strikes=[], ltp=0.0,
            futures_search_rows=[_NIFTY_FUT_ROW],
        ),
    )

    resolved = anyio.run(
        lambda: service._futures_instrument_key(
            "upstox-token", underlying_key="BSE_INDEX|SENSEX", underlying_symbol="SENSEX",
        ),
    )

    assert resolved == "NSE_FO|53216"


def test_fetch_atm_straddle_sums_ce_and_pe_ltp_at_the_closest_strike() -> None:
    option_chain_rows = [
        {
            "strike_price": 24900.0,
            "call_options": {"market_data": {"ltp": 150.0}},
            "put_options": {"market_data": {"ltp": 40.0}},
        },
        {
            "strike_price": 25000.0,
            "call_options": {"market_data": {"ltp": 120.0}},
            "put_options": {"market_data": {"ltp": 60.0}},
        },
        {
            "strike_price": 25100.0,
            "call_options": {"market_data": {"ltp": 90.0}},
            "put_options": {"market_data": {"ltp": 90.0}},
        },
    ]
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=[], daily_candles=[], strikes=[], ltp=0.0,
            option_chain_rows=option_chain_rows,
        ),
    )

    straddle = anyio.run(
        lambda: service._fetch_atm_straddle(
            "upstox-token", "NSE_INDEX|Nifty 50", "2026-07-23", 25010.0,
        ),
    )

    # 25000 is the closest strike to ltp=25010.0 -> 120.0 (CE) + 60.0 (PE).
    assert straddle == pytest.approx(180.0)


def test_fetch_atm_straddle_returns_none_without_expiry_date() -> None:
    service = UnderlyingSignalsService(
        _FakeUpstoxService(minute_candles=[], daily_candles=[], strikes=[], ltp=0.0),
    )

    straddle = anyio.run(
        lambda: service._fetch_atm_straddle("upstox-token", "NSE_INDEX|Nifty 50", None, 25010.0),
    )

    assert straddle is None


def test_fetch_atm_straddle_returns_none_when_a_side_has_no_ltp() -> None:
    option_chain_rows = [
        {"strike_price": 25000.0, "call_options": {"market_data": {"ltp": 120.0}}, "put_options": {}},
    ]
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=[], daily_candles=[], strikes=[], ltp=0.0,
            option_chain_rows=option_chain_rows,
        ),
    )

    straddle = anyio.run(
        lambda: service._fetch_atm_straddle(
            "upstox-token", "NSE_INDEX|Nifty 50", "2026-07-23", 25010.0,
        ),
    )

    assert straddle is None


def test_get_signals_includes_vwap_when_underlying_symbol_is_given() -> None:
    start = datetime(2026, 7, 18, 9, 15)
    minute_candles = _rising_candles(20, start=start, step_minutes=5)
    daily_candles = [
        ["2026-07-17T00:00:00+05:30", 100.0, 110.0, 95.0, 105.0, 500000],
    ]
    futures_candles = [
        ["2026-07-18T09:15:00+05:30", 25000.0, 25020.0, 24980.0, 25010.0, 1000],
        ["2026-07-18T09:20:00+05:30", 25010.0, 25050.0, 25000.0, 25040.0, 2000],
    ]
    service = UnderlyingSignalsService(
        _FakeUpstoxService(
            minute_candles=minute_candles,
            daily_candles=daily_candles,
            strikes=[24800.0, 24850.0, 24900.0, 24950.0],
            ltp=200.0,
            futures_search_rows=[_NIFTY_FUT_ROW],
            futures_candles=futures_candles,
            futures_ltp=25060.0,
        ),
    )

    result = anyio.run(
        lambda: service.get_signals(
            "upstox-token", underlying_key="NSE_INDEX|Nifty 50", underlying_symbol="NIFTY",
        ),
    )

    assert result["vwap"] is not None
    assert result["vwap"]["position"] == "above"
    assert any(tag.startswith("Above VWAP by ") for tag in result["tags"])


def test_get_signals_omits_vwap_when_underlying_has_no_futures_market() -> None:
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
            futures_search_rows=[],
        ),
    )

    result = anyio.run(
        lambda: service.get_signals(
            "upstox-token", underlying_key="NSE_EQ|INE002A01018", underlying_symbol="RELIANCE",
        ),
    )

    assert result["vwap"] is None

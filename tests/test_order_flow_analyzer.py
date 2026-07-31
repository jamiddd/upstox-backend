from __future__ import annotations

from app.services.order_flow_analyzer import OrderFlowAnalyzer, OrderFlowService
from app.services.upstox_market_feed_client import FeedTick, MarketDepthLevel


def _level(bid_price, bid_qty, ask_price, ask_qty):
    return MarketDepthLevel(bid_quantity=bid_qty, bid_price=bid_price, ask_quantity=ask_qty, ask_price=ask_price)


def test_first_tick_contributes_zero_ofi():
    analyzer = OrderFlowAnalyzer()
    snapshot = analyzer.on_tick([_level(100.0, 50, 100.5, 40)], 50, 40)
    assert snapshot is not None
    assert snapshot.ofi_rolling == 0.0


def test_returns_none_when_no_depth_levels():
    analyzer = OrderFlowAnalyzer()
    assert analyzer.on_tick([], None, None) is None


def test_ofi_price_improvement_counts_full_new_size():
    """Hand-computed: bid price improves 100 -> 100.5 => bid_contribution = new bid qty (60).
    Ask unchanged 100.5 -> 100.5, qty 40 -> 30 => ask_contribution = 30-40 = -10.
    ofi_contribution = bid_contribution - ask_contribution = 60 - (-10) = 70."""
    analyzer = OrderFlowAnalyzer()
    analyzer.on_tick([_level(100.0, 50, 100.5, 40)], 50, 40)
    snapshot = analyzer.on_tick([_level(100.5, 60, 100.5, 30)], 60, 30)
    assert snapshot is not None
    assert snapshot.ofi_rolling == 70.0


def test_ofi_worse_price_counts_previous_size_negatively():
    """Bid worsens 100.5 -> 100.0 => bid_contribution = -previous bid qty (-60).
    Ask improves (drops) 100.5 -> 100.0 (better for buyer... but "improve" for ask means price
    goes UP per the Kotlin source: current.askPrice > previous.askPrice). Here ask goes down
    (100.5 -> 100.0), which is the "worse" branch for ask => ask_contribution = previous ask qty
    (30). ofi = bid_contribution - ask_contribution = -60 - 30 = -90."""
    analyzer = OrderFlowAnalyzer()
    analyzer.on_tick([_level(100.0, 50, 100.5, 40)], 50, 40)
    analyzer.on_tick([_level(100.5, 60, 100.5, 30)], 60, 30)
    snapshot = analyzer.on_tick([_level(100.0, 20, 100.0, 15)], 20, 15)
    assert snapshot is not None
    # ofi_rolling is a rolling SUM across the window: 0 + 70 + this_tick's contribution.
    # this_tick contribution: bid worsens -> -60; ask worsens (price down) -> +30 (previous ask qty)
    # ofi_contribution = -60 - 30 = -90
    assert snapshot.ofi_rolling == 0.0 + 70.0 + (-90.0)


def test_ofi_window_caps_at_window_size():
    analyzer = OrderFlowAnalyzer(ofi_window_size=2)
    analyzer.on_tick([_level(100.0, 10, 100.5, 10)], 10, 10)
    analyzer.on_tick([_level(100.5, 20, 100.5, 10)], 20, 10)  # bid improves: +20
    snapshot = analyzer.on_tick([_level(101.0, 30, 100.5, 10)], 30, 10)  # bid improves: +30
    # Window size 2 -> only last two contributions summed: 20 + 30 = 50 (first 0 dropped)
    assert snapshot is not None
    assert snapshot.ofi_rolling == 50.0


def test_depth_imbalance_all_bid_is_positive_one():
    analyzer = OrderFlowAnalyzer()
    snapshot = analyzer.on_tick([_level(100.0, 100, 100.5, 0)], 100, 0)
    assert snapshot is not None
    # EMA seeded from 0: 0.3*1.0 + 0.7*0.0 = 0.3
    assert snapshot.depth_imbalance == 0.3


def test_depth_imbalance_smooths_across_ticks():
    analyzer = OrderFlowAnalyzer()
    analyzer.on_tick([_level(100.0, 100, 100.5, 0)], 100, 0)  # raw=1.0 -> smoothed=0.3
    snapshot = analyzer.on_tick([_level(100.0, 100, 100.5, 0)], 100, 0)  # raw=1.0 again
    # smoothed = 0.3*1.0 + 0.7*0.3 = 0.51
    assert snapshot is not None
    assert abs(snapshot.depth_imbalance - 0.51) < 1e-9


def test_spread_trend_stable_below_min_samples():
    analyzer = OrderFlowAnalyzer()
    snapshot = analyzer.on_tick([_level(100.0, 10, 100.5, 10)], 10, 10)
    assert snapshot is not None
    assert snapshot.spread_trend == "STABLE"


def test_spread_trend_widening():
    analyzer = OrderFlowAnalyzer(spread_history_size=9)
    snapshot = None
    # Oldest third small spread, newest third much larger spread -> WIDENING
    spreads = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 1.0, 1.0, 1.0]
    for spread in spreads:
        snapshot = analyzer.on_tick([_level(100.0, 10, 100.0 + spread, 10)], 10, 10)
    assert snapshot is not None
    assert snapshot.spread_trend == "WIDENING"


def test_iceberg_detected_after_repeated_drain_refill():
    tracker_analyzer = OrderFlowAnalyzer()
    price = 100.0
    peak = 100
    # Establish peak
    tracker_analyzer.on_tick([_level(price, peak, 100.5, 10)], peak, 10)
    suspected = False
    for _ in range(3):
        # drain below 60% of peak
        tracker_analyzer.on_tick([_level(price, 50, 100.5, 10)], 50, 10)
        # refill above 85% of peak
        snapshot = tracker_analyzer.on_tick([_level(price, 95, 100.5, 10)], 95, 10)
        suspected = snapshot.iceberg_bid_suspected if snapshot else suspected
    assert suspected is True


def test_iceberg_resets_on_price_change():
    analyzer = OrderFlowAnalyzer()
    analyzer.on_tick([_level(100.0, 100, 100.5, 10)], 100, 10)
    analyzer.on_tick([_level(100.0, 50, 100.5, 10)], 50, 10)
    analyzer.on_tick([_level(100.0, 95, 100.5, 10)], 95, 10)
    # Price changes -> resets refill count
    snapshot = analyzer.on_tick([_level(100.5, 95, 100.5, 10)], 95, 10)
    assert snapshot is not None
    assert snapshot.iceberg_bid_suspected is False


def test_bearish_absorption_detected_when_sustained_buying_fails_to_move_ask():
    """expected_direction=+1 for bearish (ask side): sustained positive OFI without ask price
    moving up should trip after `absorption_ticks_required` consecutive ticks."""
    analyzer = OrderFlowAnalyzer(absorption_ofi_threshold=10.0, absorption_ticks_required=3, ofi_window_size=1)
    ask_price = 100.5
    snapshot = None
    # First tick establishes baseline (ofi=0, doesn't count).
    analyzer.on_tick([_level(100.0, 10, ask_price, 10)], 10, 10)
    for i in range(4):
        # bid improves each tick to generate positive OFI > threshold, ask price never changes.
        snapshot = analyzer.on_tick([_level(100.0 + 0.01 * (i + 1), 100, ask_price, 10)], 100, 10)
    assert snapshot is not None
    assert snapshot.bearish_absorption_suspected is True


def test_absorption_resets_when_ofi_below_threshold():
    analyzer = OrderFlowAnalyzer(absorption_ofi_threshold=1000.0, absorption_ticks_required=2)
    analyzer.on_tick([_level(100.0, 10, 100.5, 10)], 10, 10)
    snapshot = analyzer.on_tick([_level(100.1, 10, 100.5, 10)], 10, 10)
    assert snapshot is not None
    assert snapshot.bullish_absorption_suspected is False
    assert snapshot.bearish_absorption_suspected is False


def test_order_flow_service_returns_none_without_depth():
    service = OrderFlowService()
    tick = FeedTick(instrument_key="NSE_FO|1", ltp=100.0)
    assert service.handle_tick(tick) is None


def test_order_flow_service_tracks_separately_per_instrument_key():
    service = OrderFlowService()
    tick_a1 = FeedTick(
        instrument_key="A", ltp=100.0,
        market_depth=(_level(100.0, 10, 100.5, 10),),
        total_bid_quantity=10, total_ask_quantity=10,
    )
    tick_a2 = FeedTick(
        instrument_key="A", ltp=100.5,
        market_depth=(_level(100.5, 20, 100.5, 10),),
        total_bid_quantity=20, total_ask_quantity=10,
    )
    tick_b1 = FeedTick(
        instrument_key="B", ltp=50.0,
        market_depth=(_level(50.0, 5, 50.5, 5),),
        total_bid_quantity=5, total_ask_quantity=5,
    )
    service.handle_tick(tick_a1)
    snapshot_a2 = service.handle_tick(tick_a2)
    snapshot_b1 = service.handle_tick(tick_b1)
    assert snapshot_a2 is not None
    assert snapshot_a2.ofi_rolling != 0.0
    # Instrument B's first-ever tick contributes 0 OFI, independent of A's accumulated state.
    assert snapshot_b1 is not None
    assert snapshot_b1.ofi_rolling == 0.0

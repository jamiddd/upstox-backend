from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal, Optional

from app.services.upstox_market_feed_client import FeedTick, MarketDepthLevel

# Direct port of OrderFlowAnalyzer.kt (Android's Contract Depth screen). Order-flow imbalance is
# inherently a *flow*: each tick's contribution depends on the *previous* tick's top-of-book, and
# is meaningfully summed over a short rolling window, not read off one snapshot alone -- unlike
# this backend's other derived-analytics classes (GexCalculator/OiCalculator-equivalents, which
# are pure functions over one snapshot), this needs real per-instrument state. Ported server-side
# (not to TypeScript too) specifically so both Android and the web client see numerically identical
# output from one computation -- see WEB_CLIENT_ROADMAP.md's M2 entry for why.
#
# ofiWindowSize/imbalanceSmoothingAlpha/spreadHistorySize/absorption thresholds are initial
# choices, not measured constants -- same "expect to tune after watching live" posture the Kotlin
# source's own doc comments give.

SpreadTrend = Literal["WIDENING", "NARROWING", "STABLE"]

_MIN_SAMPLES_FOR_TREND = 6
_TREND_THRESHOLD = 0.10
_PRICE_MOVE_EPSILON = 1e-6


@dataclass(frozen=True)
class OrderFlowSnapshot:
    ofi_rolling: float
    depth_imbalance: float
    spread: float
    spread_trend: SpreadTrend
    iceberg_bid_suspected: bool
    iceberg_ask_suspected: bool
    bullish_absorption_suspected: bool
    bearish_absorption_suspected: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "ofi_rolling": self.ofi_rolling,
            "depth_imbalance": self.depth_imbalance,
            "spread": self.spread,
            "spread_trend": self.spread_trend,
            "iceberg_bid_suspected": self.iceberg_bid_suspected,
            "iceberg_ask_suspected": self.iceberg_ask_suspected,
            "bullish_absorption_suspected": self.bullish_absorption_suspected,
            "bearish_absorption_suspected": self.bearish_absorption_suspected,
        }


@dataclass(frozen=True)
class _TopOfBook:
    bid_price: float
    bid_quantity: float
    ask_price: float
    ask_quantity: float


class _IcebergSideTracker:
    """Heuristic iceberg/hidden-order detector for one side (bid or ask) of the top of book: a
    displayed quantity that repeatedly drains well below its own recent peak, then refills back up
    close to that peak, several times in a row, without the price itself ever moving."""

    def __init__(
        self,
        drop_fraction: float = 0.6,
        refill_fraction: float = 0.85,
        refills_required: int = 3,
    ) -> None:
        self._drop_fraction = drop_fraction
        self._refill_fraction = refill_fraction
        self._refills_required = refills_required
        self._anchor_price: Optional[float] = None
        self._peak_quantity = 0.0
        self._awaiting_refill = False
        self._refill_count = 0

    def on_update(self, price: float, quantity: float) -> bool:
        if price != self._anchor_price:
            self._anchor_price = price
            self._peak_quantity = quantity
            self._awaiting_refill = False
            self._refill_count = 0
            return False
        if not self._awaiting_refill:
            if quantity <= self._peak_quantity * self._drop_fraction:
                self._awaiting_refill = True
            elif quantity > self._peak_quantity:
                self._peak_quantity = quantity
        elif quantity >= self._peak_quantity * self._refill_fraction:
            self._refill_count += 1
            self._awaiting_refill = False
        return self._refill_count >= self._refills_required


class _AbsorptionTracker:
    """Heuristic absorption detector for one side of the top of book: sustained one-directional
    order flow that fails to move the corresponding best price in that pressure's own favor, over
    a run of ticks. `expected_direction` is +1 for the ask side (sustained buying pressure should
    push the ask up), -1 for the bid side (sustained selling pressure should push the bid down)."""

    def __init__(self, ofi_threshold: float, ticks_required: int, expected_direction: int) -> None:
        self._ofi_threshold = ofi_threshold
        self._ticks_required = ticks_required
        self._expected_direction = expected_direction
        self._anchor_price: Optional[float] = None
        self._sustained_ticks = 0

    def on_update(self, ofi_rolling: float, price: float) -> bool:
        pressure_aligned = ofi_rolling * self._expected_direction
        if pressure_aligned < self._ofi_threshold:
            self._anchor_price = None
            self._sustained_ticks = 0
            return False
        previous_anchor = self._anchor_price
        self._anchor_price = price
        if previous_anchor is None:
            self._sustained_ticks = 1
        elif (price - previous_anchor) * self._expected_direction > _PRICE_MOVE_EPSILON:
            # Price moved in the pressure's own favor -- the flow broke through, so this run's
            # absorption thesis is invalidated, not just paused; start over from this tick.
            self._sustained_ticks = 0
        else:
            self._sustained_ticks += 1
        return self._sustained_ticks >= self._ticks_required


class OrderFlowAnalyzer:
    """One instance per instrument_key -- see OrderFlowService for the per-key registry."""

    def __init__(
        self,
        ofi_window_size: int = 50,
        imbalance_smoothing_alpha: float = 0.3,
        spread_history_size: int = 20,
        absorption_ofi_threshold: float = 150.0,
        absorption_ticks_required: int = 8,
    ) -> None:
        self._ofi_window_size = ofi_window_size
        self._imbalance_smoothing_alpha = imbalance_smoothing_alpha
        self._spread_history_size = spread_history_size
        self._previous_top: Optional[_TopOfBook] = None
        self._ofi_window: deque[float] = deque()
        self._smoothed_imbalance = 0.0
        self._spread_history: deque[float] = deque()
        self._bid_iceberg = _IcebergSideTracker()
        self._ask_iceberg = _IcebergSideTracker()
        self._bullish_absorption = _AbsorptionTracker(
            absorption_ofi_threshold, absorption_ticks_required, expected_direction=-1,
        )
        self._bearish_absorption = _AbsorptionTracker(
            absorption_ofi_threshold, absorption_ticks_required, expected_direction=1,
        )

    def on_tick(
        self,
        levels: list[MarketDepthLevel],
        total_bid_quantity: Optional[float],
        total_ask_quantity: Optional[float],
    ) -> Optional[OrderFlowSnapshot]:
        """Feeds one tick's depth levels in. Returns None if `levels` has no top-of-book to read
        (an LTP-only tick with no depth payload) -- callers should keep showing whatever snapshot
        they last had rather than treating this as a reset."""
        if not levels:
            return None
        # levels[0] is the top-of-book pair (best bid vs best ask) -- same ordering
        # decode_feed_response already produces (worst-to-best outward from the spread).
        top = levels[0]

        self._ofi_window.append(self._compute_ofi_contribution(self._previous_top, top))
        if len(self._ofi_window) > self._ofi_window_size:
            self._ofi_window.popleft()
        self._previous_top = _TopOfBook(top.bid_price, top.bid_quantity, top.ask_price, top.ask_quantity)
        ofi_rolling = sum(self._ofi_window)

        total_bid = total_bid_quantity if total_bid_quantity is not None else sum(
            level.bid_quantity for level in levels
        )
        total_ask = total_ask_quantity if total_ask_quantity is not None else sum(
            level.ask_quantity for level in levels
        )
        raw_imbalance = (total_bid - total_ask) / (total_bid + total_ask) if (total_bid + total_ask) > 0 else 0.0
        self._smoothed_imbalance = (
            self._imbalance_smoothing_alpha * raw_imbalance
            + (1 - self._imbalance_smoothing_alpha) * self._smoothed_imbalance
        )

        spread = top.ask_price - top.bid_price
        self._spread_history.append(spread)
        if len(self._spread_history) > self._spread_history_size:
            self._spread_history.popleft()

        iceberg_bid = self._bid_iceberg.on_update(top.bid_price, top.bid_quantity)
        iceberg_ask = self._ask_iceberg.on_update(top.ask_price, top.ask_quantity)
        bullish_absorption = self._bullish_absorption.on_update(ofi_rolling, top.bid_price)
        bearish_absorption = self._bearish_absorption.on_update(ofi_rolling, top.ask_price)

        return OrderFlowSnapshot(
            ofi_rolling=ofi_rolling,
            depth_imbalance=self._smoothed_imbalance,
            spread=spread,
            spread_trend=self._spread_trend(),
            iceberg_bid_suspected=iceberg_bid,
            iceberg_ask_suspected=iceberg_ask,
            bullish_absorption_suspected=bullish_absorption,
            bearish_absorption_suspected=bearish_absorption,
        )

    @staticmethod
    def _compute_ofi_contribution(previous: Optional[_TopOfBook], current: MarketDepthLevel) -> float:
        """Cont-Kukanov-Stoikov top-of-book order flow imbalance: a price improvement on a side
        counts that side's full new size; an unchanged price counts only the size delta; a price
        that got worse counts the previous size negatively (that liquidity is gone). `previous`
        None (the very first tick this analyzer has ever seen) contributes exactly 0."""
        if previous is None:
            return 0.0
        if current.bid_price > previous.bid_price:
            bid_contribution = current.bid_quantity
        elif current.bid_price == previous.bid_price:
            bid_contribution = current.bid_quantity - previous.bid_quantity
        else:
            bid_contribution = -previous.bid_quantity

        if current.ask_price > previous.ask_price:
            ask_contribution = -previous.ask_quantity
        elif current.ask_price == previous.ask_price:
            ask_contribution = current.ask_quantity - previous.ask_quantity
        else:
            ask_contribution = previous.ask_quantity

        return bid_contribution - ask_contribution

    def _spread_trend(self) -> SpreadTrend:
        """Compares the oldest third of the spread history against the newest third, past a small
        relative threshold, so ordinary tick-to-tick spread noise doesn't flicker the label."""
        history = list(self._spread_history)
        if len(history) < _MIN_SAMPLES_FOR_TREND:
            return "STABLE"
        third = max(len(history) // 3, 1)
        oldest_avg = sum(history[:third]) / third
        newest_avg = sum(history[-third:]) / third
        if oldest_avg <= 0.0:
            return "STABLE"
        relative_change = (newest_avg - oldest_avg) / oldest_avg
        if relative_change > _TREND_THRESHOLD:
            return "WIDENING"
        if relative_change < -_TREND_THRESHOLD:
            return "NARROWING"
        return "STABLE"


class OrderFlowService:
    """Per-instrument-key registry of `OrderFlowAnalyzer` instances, invoked from
    `app.main._on_market_tick` for every tick that carries depth data. No eviction: D30
    subscriptions are already capped small by Upstox's own limits (the same constraint
    `FeedSubscriptionManager` already enforces elsewhere), so unbounded-but-small per-key growth
    over the process lifetime is acceptable."""

    def __init__(self) -> None:
        self._analyzers: dict[str, OrderFlowAnalyzer] = {}

    def handle_tick(self, tick: FeedTick) -> Optional[OrderFlowSnapshot]:
        if not tick.market_depth:
            return None
        analyzer = self._analyzers.setdefault(tick.instrument_key, OrderFlowAnalyzer())
        return analyzer.on_tick(
            list(tick.market_depth), tick.total_bid_quantity, tick.total_ask_quantity,
        )

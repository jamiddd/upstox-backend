from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from app.core.exceptions import TokenStoreError, UpstoxAuthRequiredError
from app.generated import MarketDataFeed_pb2 as pb
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService
from app.services.upstox_ws_client import UpstoxAuthPendingError, UpstoxWebSocketClient

logger = logging.getLogger(__name__)

# The backend requests Upstox's deepest live mode for full subscriptions. `full_d30` includes the
# same LTPC/OHLC/Greeks payload as `full`, plus up to 30 market levels; Upstox gates this mode
# behind Plus entitlement. Android still decides whether to render only the top five or let the
# user expand the complete received book.
MODE_LTPC = "ltpc"
MODE_FULL = "full_d30"


@dataclass(frozen=True)
class FeedCandle:
    """One live OHLC bar supplied directly by Upstox's V3 full market feed. Mirrors Android's
    `FeedCandle` field-for-field so downstream candle-merge logic (`live_candle_builder.py`) can
    be a direct port of the already-proven Kotlin implementation."""

    timestamp_millis: int
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class MarketDepthLevel:
    bid_quantity: int
    bid_price: float
    ask_quantity: int
    ask_price: float


@dataclass(frozen=True)
class FeedTick:
    """A simplified, backend-friendly view of one instrument's data from a decoded Upstox market
    feed message. Mirrors Android's `FeedTick` field-for-field -- see that class's own doc comment
    for why the raw protobuf `FeedResponse` needs flattening in the first place."""

    instrument_key: str
    ltp: Optional[float]
    last_trade_time_millis: Optional[int] = None
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    market_depth: tuple[MarketDepthLevel, ...] = ()
    total_bid_quantity: Optional[int] = None
    total_ask_quantity: Optional[int] = None
    one_minute_candle: Optional[FeedCandle] = None


def decode_feed_response(data: bytes) -> list[FeedTick]:
    """Turns one raw protobuf `FeedResponse` message into zero or more `FeedTick`s -- one per
    instrument included in the message. Direct port of Android's `MarketFeedClient
    .decodeFeedResponse`/`decodeFullFeed` -- same field mapping, same three `Feed` shapes
    (`ltpc`, `fullFeed.marketFF`, `fullFeed.indexFF`), same fields ignored (volume/OI/Greeks
    beyond what the one-minute candle already carries)."""
    try:
        feed_response = pb.FeedResponse.FromString(data)
    except Exception:
        logger.warning("Failed to parse market feed message", exc_info=True)
        return []

    ticks: list[FeedTick] = []
    for instrument_key, feed in feed_response.feeds.items():
        union = feed.WhichOneof("FeedUnion")
        if union == "ltpc":
            ticks.append(
                FeedTick(
                    instrument_key=instrument_key,
                    ltp=feed.ltpc.ltp,
                    last_trade_time_millis=feed.ltpc.ltt,
                ),
            )
        elif union == "fullFeed":
            tick = _decode_full_feed(instrument_key, feed.fullFeed)
            if tick is not None:
                ticks.append(tick)
        # else: firstLevelWithGreeks or unset -- not used by this backend.
    return ticks


def _decode_full_feed(instrument_key: str, full_feed: Any) -> Optional[FeedTick]:
    union = full_feed.WhichOneof("FullFeedUnion")
    if union == "marketFF":
        market_full_feed = full_feed.marketFF
        # "Best" bid/ask is the first entry in the depth-of-market quote list.
        quotes = market_full_feed.marketLevel.bidAskQuote
        top_of_book = quotes[0] if len(quotes) > 0 else None
        market_depth = tuple(
            MarketDepthLevel(
                bid_quantity=quote.bidQ,
                bid_price=quote.bidP,
                ask_quantity=quote.askQ,
                ask_price=quote.askP,
            )
            for quote in quotes
        )
        return FeedTick(
            instrument_key=instrument_key,
            ltp=market_full_feed.ltpc.ltp,
            last_trade_time_millis=market_full_feed.ltpc.ltt,
            bid_price=top_of_book.bidP if top_of_book is not None else None,
            ask_price=top_of_book.askP if top_of_book is not None else None,
            market_depth=market_depth,
            total_bid_quantity=int(market_full_feed.tbq),
            total_ask_quantity=int(market_full_feed.tsq),
            one_minute_candle=_one_minute_candle(market_full_feed.marketOHLC.ohlc),
        )
    if union == "indexFF":
        # Indices (e.g. NIFTY 50) have no bid/ask -- they aren't directly tradeable.
        index_full_feed = full_feed.indexFF
        return FeedTick(
            instrument_key=instrument_key,
            ltp=index_full_feed.ltpc.ltp,
            last_trade_time_millis=index_full_feed.ltpc.ltt,
            one_minute_candle=_one_minute_candle(index_full_feed.marketOHLC.ohlc),
        )
    return None


def _one_minute_candle(ohlc_list: Any) -> Optional[FeedCandle]:
    for ohlc in ohlc_list:
        if ohlc.interval == "I1":
            return FeedCandle(
                timestamp_millis=ohlc.ts,
                open=ohlc.open,
                high=ohlc.high,
                low=ohlc.low,
                close=ohlc.close,
                volume=ohlc.vol,
            )
    return None


class UpstoxMarketFeedClient:
    """Owns the backend's single persistent connection to Upstox's V3 market-data feed --
    replaces the REST-polling data source for tick/candle-derived values with a live push feed,
    the same protocol Android's `MarketFeedClient.kt` already implements client-side (this class
    is deliberately structured to mirror it closely: same subscribe/unsubscribe semantics, same
    desired-subscription-set-remembered-and-resent-on-reconnect design, same protobuf decode).

    `on_tick` is called for every decoded `FeedTick` -- wired up to `FeedSubscriptionManager`/
    `live_candle_builder.py` by whoever constructs this client (see `app.main`'s lifespan).
    """

    def __init__(
        self,
        *,
        upstox: UpstoxService,
        token_store: EncryptedTokenStore,
        on_tick: Callable[[FeedTick], None],
        on_state_change: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._upstox = upstox
        self._token_store = token_store
        self._on_tick = on_tick
        self._desired_full: list[str] = []
        self._desired_ltpc: list[str] = []
        self._client = UpstoxWebSocketClient(
            name="UpstoxMarketFeedClient",
            authorize=self._authorize,
            on_message=self._on_message,
            desired_subscriptions=self._desired_subscription_messages,
            on_state_change=on_state_change,
        )

    def start(self) -> None:
        self._client.start()

    async def stop(self) -> None:
        await self._client.stop()

    @property
    def connected(self) -> bool:
        return self._client.connected

    async def _authorize(self) -> str:
        try:
            access_token = self._token_store.load_access_token()
        except (TokenStoreError, UpstoxAuthRequiredError) as exc:
            raise UpstoxAuthPendingError("Upstox login is required for the market feed") from exc
        payload = await self._upstox.get_market_feed_authorize(access_token)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("authorized_redirect_uri"), str):
            raise RuntimeError("Unexpected market feed authorization response")
        return data["authorized_redirect_uri"]

    def _on_message(self, message: Any) -> None:
        # Upstox always sends market data as binary protobuf frames.
        if not isinstance(message, (bytes, bytearray)):
            logger.warning("Unexpected non-binary frame from market feed")
            return
        for tick in decode_feed_response(bytes(message)):
            self._on_tick(tick)

    def _desired_subscription_messages(self) -> list[dict[str, Any]]:
        messages = []
        if self._desired_full:
            messages.append(_control_message("sub", MODE_FULL, self._desired_full))
        if self._desired_ltpc:
            messages.append(_control_message("sub", MODE_LTPC, self._desired_ltpc))
        return messages

    async def replace_full_subscription(self, instrument_keys: list[str]) -> None:
        """Replaces the full-mode watch set without interrupting instruments common to the old
        and new sets -- direct port of Android's `MarketFeedClient.replaceFullSubscription`."""
        desired = list(dict.fromkeys(instrument_keys))
        previous = self._desired_full
        if desired == previous:
            return

        removed = [key for key in previous if key not in desired]
        added = [key for key in desired if key not in previous]
        self._desired_full = desired

        if removed:
            await self._client.send_json(_control_message("unsub", MODE_LTPC, removed))
            retained_ltpc = [key for key in removed if key in self._desired_ltpc]
            if retained_ltpc:
                await self._client.send_json(_control_message("sub", MODE_LTPC, retained_ltpc))
        if added:
            await self._client.send_json(_control_message("sub", MODE_FULL, added))

    async def subscribe_ltpc(self, instrument_keys: list[str]) -> None:
        if not instrument_keys:
            return
        self._desired_ltpc = instrument_keys
        await self._client.send_json(_control_message("sub", MODE_LTPC, instrument_keys))

    async def unsubscribe(self, instrument_keys: list[str]) -> None:
        to_remove = set(instrument_keys)
        self._desired_full = [key for key in self._desired_full if key not in to_remove]
        self._desired_ltpc = [key for key in self._desired_ltpc if key not in to_remove]
        await self._client.send_json(_control_message("unsub", MODE_LTPC, list(instrument_keys)))


def _control_message(method: str, mode: str, instrument_keys: list[str]) -> dict[str, Any]:
    """Same JSON shape as Android's `FeedControlMessage`/`FeedSubscriptionData`."""
    return {
        "guid": str(uuid.uuid4()),
        "method": method,
        "data": {"mode": mode, "instrumentKeys": instrument_keys},
    }

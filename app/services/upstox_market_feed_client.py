from __future__ import annotations

import logging
import time
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

# The backend requests Upstox's normal (non-Plus) full-depth mode: LTPC/OHLC/Greeks payload plus
# the top 5 market levels. Upstox Plus's `full_d30` tier (up to 30 levels, 50-instrument-key cap)
# was tried and reverted -- see git history -- may revisit later once its subscription-leak
# hardening is done.
MODE_LTPC = "ltpc"
MODE_FULL = "full"

# How many consecutive resend_stale_subscriptions passes the same key can need a plain re-`sub`
# before escalating to an explicit unsub-then-sub -- see that function's own doc comment.
_ESCALATE_AFTER_NUDGES = 2


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
        # Per-instrument last-tick-seen time (monotonic -- immune to wall-clock/NTP jumps, same
        # convention as tracked_instruments_poller.py's own last-polled tracking). Backs
        # resend_stale_subscriptions' self-heal for a silently-dropped single instrument, which the
        # connection-wide stale-frame watchdog in UpstoxWebSocketClient can't detect on its own
        # (that one only notices when NO instrument has ticked in a while, not when a single
        # subscribed instrument specifically goes quiet while everything else keeps flowing).
        self._last_seen_monotonic: dict[str, float] = {}
        # How many *consecutive* resend_stale_subscriptions passes a key has needed nudging in a
        # row -- backs that function's own escalation from a plain duplicate `sub` (cheap, fixes a
        # transient drop) to an explicit unsub-then-sub (see its own doc comment for why that's
        # sometimes needed). Cleared the moment a key stops being stale, so an old streak never
        # carries into some later, unrelated incident.
        self._consecutive_nudge_count: dict[str, int] = {}
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
        # Upstox always sends market data as binary protobuf frames -- a non-binary frame is the
        # shape Upstox uses for sub/unsub acks and rejections (e.g. exceeding the full-mode
        # combined-category cap, a bad instrument key, rate limiting). This backend has no other
        # visibility into those, so logging the actual content (not just that one arrived) is what
        # makes a real rejection diagnosable instead of silently invisible -- see
        # resend_stale_subscriptions for the self-heal this is paired with. Truncated defensively
        # in case Upstox ever sends something unexpectedly large as a text frame.
        if not isinstance(message, (bytes, bytearray)):
            logger.warning("Unexpected non-binary frame from market feed: %r", str(message)[:2000])
            return
        for tick in decode_feed_response(bytes(message)):
            self._last_seen_monotonic[tick.instrument_key] = time.monotonic()
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
            # FIX: only genuinely-dropped keys (not staying on in ltpc mode) had their staleness
            # tracking cleaned up here before -- actually never did at all, so a key that rotated
            # out of a shifting window (e.g. the chart's own neighbor-strike window following
            # smart-strike selection) lingered in these dicts indefinitely, showing up as
            # permanently "stale" clutter in `debug_snapshot()` even though it's no longer desired
            # at all. `unsubscribe()` already does this same cleanup for its own callers.
            retained_ltpc_set = set(retained_ltpc)
            for key in removed:
                if key in retained_ltpc_set:
                    continue
                self._last_seen_monotonic.pop(key, None)
                self._consecutive_nudge_count.pop(key, None)
        if added:
            await self._client.send_json(_control_message("sub", MODE_FULL, added))
            # Seed rather than leave unset -- a just-subscribed instrument hasn't had a chance to
            # tick yet, and resend_stale_subscriptions would otherwise wrongly flag it stale before
            # the check interval even gives Upstox a chance to start sending it.
            now = time.monotonic()
            for key in added:
                self._last_seen_monotonic.setdefault(key, now)

    async def subscribe_ltpc(self, instrument_keys: list[str]) -> None:
        if not instrument_keys:
            return
        self._desired_ltpc = instrument_keys
        await self._client.send_json(_control_message("sub", MODE_LTPC, instrument_keys))
        # Same seeding reasoning as replace_full_subscription's `added` handling above.
        now = time.monotonic()
        for key in instrument_keys:
            self._last_seen_monotonic.setdefault(key, now)

    async def unsubscribe(self, instrument_keys: list[str]) -> None:
        to_remove = set(instrument_keys)
        self._desired_full = [key for key in self._desired_full if key not in to_remove]
        self._desired_ltpc = [key for key in self._desired_ltpc if key not in to_remove]
        # Stop tracking staleness for anything no longer desired at all -- keeps these dicts from
        # growing unboundedly as positions/contracts rotate in and out over a long-running process.
        for key in to_remove:
            self._last_seen_monotonic.pop(key, None)
            self._consecutive_nudge_count.pop(key, None)
        await self._client.send_json(_control_message("unsub", MODE_LTPC, list(instrument_keys)))

    async def resend_stale_subscriptions(self, stale_after_seconds: float) -> list[str]:
        """Best-effort self-heal for a silently-dropped single-instrument subscription.

        Upstox's sub/unsub control messages are fire-and-forget (see `_on_message`'s own doc
        comment) -- this backend has no way to know a specific instrument's subscription was
        rejected or otherwise dropped, short of noticing the symptom: no ticks for something we
        asked for. `UpstoxWebSocketClient`'s own connection-wide stale-frame watchdog can't catch
        this either, since it only resets on ANY frame from ANY instrument -- one instrument going
        silent while everything else keeps ticking normally never trips it.

        First nudge for a key is a plain re-`sub` -- the same wire message
        `replace_full_subscription`/`subscribe_ltpc` already send, so Upstox sees an ordinary
        duplicate subscribe, nothing exotic, and a transient drop clears. FIX: confirmed live
        (via `GET /api/debug/feed-status`) that a persistently-stuck instrument can go a full
        subsequent stale window with *still* zero ticks after a plain re-`sub` -- Upstox appears to
        silently dedupe a repeated subscribe for something it (wrongly) still thinks is already
        active. Once the *same* key has needed nudging [_ESCALATE_AFTER_NUDGES] times in a row,
        this escalates to an explicit `unsub` before the re-`sub` -- a stronger reset than a bare
        duplicate subscribe. Resets each nudged key's timestamp (and bumps its consecutive-nudge
        streak) so this stays self-limiting: nudged at most once per [stale_after_seconds] window,
        not on every check, and the streak resets the moment a key stops being stale.

        Returns the nudged keys (both modes combined) purely so the caller can log which
        instruments actually needed a self-heal -- the confirmation signal that this mechanism is
        catching real incidents rather than never firing.
        """
        if not self._client.connected:
            return []

        now = time.monotonic()

        def is_stale(key: str) -> bool:
            return now - self._last_seen_monotonic.get(key, now) >= stale_after_seconds

        stale_full = [key for key in self._desired_full if is_stale(key)]
        stale_full_set = set(stale_full)
        # Full wins per key if somehow stale in both -- same precedence
        # FeedSubscriptionManager._apply already uses when a key is desired in both modes.
        stale_ltpc = [
            key for key in self._desired_ltpc if key not in stale_full_set and is_stale(key)
        ]
        nudged = stale_full + stale_ltpc
        nudged_set = set(nudged)

        # A key that's recovered (no longer stale) shouldn't carry its escalation streak into
        # some future, unrelated staleness incident.
        for key in list(self._consecutive_nudge_count):
            if key not in nudged_set:
                del self._consecutive_nudge_count[key]

        escalate_set = {
            key for key in nudged
            if self._consecutive_nudge_count.get(key, 0) >= _ESCALATE_AFTER_NUDGES
        }
        escalate_full = [key for key in stale_full if key in escalate_set]
        escalate_ltpc = [key for key in stale_ltpc if key in escalate_set]
        plain_full = [key for key in stale_full if key not in escalate_set]
        plain_ltpc = [key for key in stale_ltpc if key not in escalate_set]

        if escalate_full or escalate_ltpc:
            logger.warning(
                "Market feed self-heal: escalating to unsub+resub for %s (still stale after a "
                "plain resend)",
                escalate_full + escalate_ltpc,
            )
            await self._client.send_json(
                _control_message("unsub", MODE_LTPC, escalate_full + escalate_ltpc),
            )
            if escalate_full:
                await self._client.send_json(_control_message("sub", MODE_FULL, escalate_full))
            if escalate_ltpc:
                await self._client.send_json(_control_message("sub", MODE_LTPC, escalate_ltpc))
        if plain_full:
            await self._client.send_json(_control_message("sub", MODE_FULL, plain_full))
        if plain_ltpc:
            await self._client.send_json(_control_message("sub", MODE_LTPC, plain_ltpc))

        for key in nudged:
            self._last_seen_monotonic[key] = now
            self._consecutive_nudge_count[key] = self._consecutive_nudge_count.get(key, 0) + 1

        return nudged

    def debug_snapshot(self) -> dict[str, Any]:
        """Read-only diagnostic view of this client's current state -- see `api/routes.py`'s
        `GET /api/debug/feed-status`, added so a reported "frozen contract" can be cross-checked
        against the backend's own view of what's desired and each key's own last-tick age,
        without needing direct server/log access."""
        now = time.monotonic()
        seconds_since_last_tick = {
            key: round(now - seen, 1) for key, seen in self._last_seen_monotonic.items()
        }
        return {
            "connected": self.connected,
            "desired_full": list(self._desired_full),
            "desired_full_count": len(self._desired_full),
            "desired_ltpc": list(self._desired_ltpc),
            "seconds_since_last_tick": dict(
                sorted(seconds_since_last_tick.items(), key=lambda item: -item[1]),
            ),
        }


def _control_message(method: str, mode: str, instrument_keys: list[str]) -> dict[str, Any]:
    """Same JSON shape as Android's `FeedControlMessage`/`FeedSubscriptionData`."""
    return {
        "guid": str(uuid.uuid4()),
        "method": method,
        "data": {"mode": mode, "instrumentKeys": instrument_keys},
    }

from __future__ import annotations

from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.upstox_market_feed_client import UpstoxMarketFeedClient


class FeedSubscriptionManager:
    """Computes the union of everything the backend's single market-data feed connection needs to
    watch, and diff-subscribes against `UpstoxMarketFeedClient` accordingly.

    Two independent sources feed that union:
    1. **Always-needed** -- the user's Settings-picked tracked underlyings (`TrackedInstrumentsStore`),
       for background EMA/VWAP/opening-range/pivot computation independent of whether any app
       session is currently connected.
    2. **Live-client-wanted** -- whatever each currently-connected app session's Main screen/chart
       currently cares about (selected/pinned contract, nearby-strike window, open positions,
       watchlist), registered per session id so multiple connections don't clobber each other's
       wants and a disconnecting session's contribution can be cleanly removed.

    Full-mode wins over LTPC when both are requested for the same key (matches Android's own
    `MarketFeedClient` posture: `replaceFullSubscription`'s LTPC-retention only concerns keys
    actually still wanted in LTPC after leaving full mode, never the reverse).
    """

    def __init__(
        self,
        *,
        market_feed_client: UpstoxMarketFeedClient,
        tracked_store: TrackedInstrumentsStore,
    ) -> None:
        self._market_feed_client = market_feed_client
        self._tracked_store = tracked_store
        self._client_full: dict[str, set[str]] = {}
        self._client_ltpc: dict[str, set[str]] = {}
        # UpstoxMarketFeedClient.subscribe_ltpc() -- like Android's own subscribeLtpc() -- always
        # resends "sub" for its whole argument without unsubscribing anything dropped from it.
        # Android's own callers (updatePositionSubscription/updateWatchlistSubscription) handle
        # that by explicitly unsubscribing the previous full set before subscribing the new one
        # whenever it changes; mirrored here rather than reinventing a finer-grained diff for a
        # set this cheap to fully replace.
        self._previous_ltpc_union: set[str] = set()

    async def refresh_tracked_instruments(self) -> None:
        """Re-applies the union after the tracked-instruments list itself changes (or on a
        periodic tick, same cadence as the background poller already re-checks it)."""
        await self._apply()

    async def set_client_subscription(
        self,
        session_id: str,
        *,
        full: list[str],
        ltpc: list[str],
    ) -> None:
        """Replaces one connected app session's own wanted instrument sets."""
        self._client_full[session_id] = set(full)
        self._client_ltpc[session_id] = set(ltpc)
        await self._apply()

    async def remove_client(self, session_id: str) -> None:
        """Drops a disconnected session's contribution to the union entirely."""
        self._client_full.pop(session_id, None)
        self._client_ltpc.pop(session_id, None)
        await self._apply()

    async def _apply(self) -> None:
        full_union: set[str] = set(self._tracked_store.load())
        for keys in self._client_full.values():
            full_union |= keys

        ltpc_union: set[str] = set()
        for keys in self._client_ltpc.values():
            ltpc_union |= keys
        ltpc_union -= full_union

        await self._market_feed_client.replace_full_subscription(sorted(full_union))
        if ltpc_union != self._previous_ltpc_union:
            if self._previous_ltpc_union:
                await self._market_feed_client.unsubscribe(sorted(self._previous_ltpc_union))
            if ltpc_union:
                await self._market_feed_client.subscribe_ltpc(sorted(ltpc_union))
            self._previous_ltpc_union = ltpc_union

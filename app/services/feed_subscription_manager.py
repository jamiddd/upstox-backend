from __future__ import annotations

import logging

from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.upstox_market_feed_client import UpstoxMarketFeedClient

logger = logging.getLogger(__name__)

# Upstox's own full_d30 cap (see UpstoxMarketFeedClient._on_message's own doc comment) -- nothing
# here enforces it, this is purely so a union approaching it shows up in logs *before* Upstox
# starts rejecting subscriptions, rather than only being diagnosable after the fact from a
# rejection frame.
_FULL_MODE_CAP = 50
_FULL_MODE_CAP_WARN_THRESHOLD = 40


class FeedSubscriptionManager:
    """Computes the union of everything the backend's single market-data feed connection needs to
    watch, and diff-subscribes against `UpstoxMarketFeedClient` accordingly.

    Three independent sources feed that union:
    1. **Always-needed (tracked)** -- the user's Settings-picked tracked underlyings
       (`TrackedInstrumentsStore`), for background EMA/VWAP/opening-range/pivot computation
       independent of whether any app session is currently connected.
    2. **Always-needed (open positions)** -- every currently open position's own instrument (see
       `set_open_position_instruments`), LTPC level only, so `PositionPnlTracker`/
       `max_loss_watcher` get live ticks to react to regardless of whether any app session
       happens to have that instrument open too -- this is what makes the backend's own max-loss
       watcher tick-driven instead of a REST-polling loop.
    3. **Live-client-wanted** -- whatever each currently-connected app session's Main screen/chart
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
        self._position_instruments: set[str] = set()
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

    async def set_open_position_instruments(self, instrument_keys: set[str]) -> None:
        """Replaces the always-needed open-position set -- called by whatever refreshes
        `PositionPnlTracker` (see that class's own doc comment) each time it does, so a newly
        opened position starts getting live ticks immediately and a closed one stops."""
        self._position_instruments = instrument_keys
        await self._apply()

    async def _apply(self) -> None:
        full_union: set[str] = set(self._tracked_store.load())
        for keys in self._client_full.values():
            full_union |= keys

        if len(full_union) >= _FULL_MODE_CAP:
            logger.warning(
                "Full-mode subscription union at %d instruments, at/over Upstox's %d cap -- "
                "expect rejected subscriptions",
                len(full_union),
                _FULL_MODE_CAP,
            )
        elif len(full_union) >= _FULL_MODE_CAP_WARN_THRESHOLD:
            logger.info(
                "Full-mode subscription union at %d/%d instruments, approaching Upstox's cap",
                len(full_union),
                _FULL_MODE_CAP,
            )

        ltpc_union: set[str] = set(self._position_instruments)
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

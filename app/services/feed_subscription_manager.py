from __future__ import annotations

import asyncio
import logging

from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.upstox_market_feed_client import UpstoxMarketFeedClient

logger = logging.getLogger(__name__)

# Upstox's Normal-tier *combined* subscription limit (this connection subscribes both `full` and
# `ltpc` concurrently, so the combined 1500 cap applies, not the 2000 individual-category limit --
# see the Normal Connection and Subscription Limits table). Nothing here enforces it, this is
# purely so a union approaching it shows up in logs *before* Upstox starts rejecting
# subscriptions, rather than only being diagnosable after the fact from a rejection frame.
_FULL_MODE_CAP = 1500
_FULL_MODE_CAP_WARN_THRESHOLD = 1200
# Keep operational headroom below Upstox Plus's hard 50-key Full D30 limit for transitions and
# reconciliation. D30 is reserved exclusively for live client viewports; tracked instruments and
# open positions remain on normal full/LTPC modes.
_FULL_D30_APPLICATION_CAP = 45


class D30SubscriptionLimitError(ValueError):
    pass


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
        self._client_d30: dict[str, set[str]] = {}
        self._client_full: dict[str, set[str]] = {}
        self._client_ltpc: dict[str, set[str]] = {}
        self._position_instruments: set[str] = set()
        self._apply_lock = asyncio.Lock()

    async def refresh_tracked_instruments(self) -> None:
        """Re-applies the union after the tracked-instruments list itself changes (or on a
        periodic tick, same cadence as the background poller already re-checks it)."""
        async with self._apply_lock:
            await self._apply()

    async def set_client_subscription(
        self,
        session_id: str,
        *,
        d30: list[str] | None = None,
        full: list[str],
        ltpc: list[str],
    ) -> None:
        """Replaces one connected app session's own wanted instrument sets."""
        async with self._apply_lock:
            proposed_d30 = set(d30 or [])
            projected_d30_union = proposed_d30.copy()
            for other_session_id, keys in self._client_d30.items():
                if other_session_id != session_id:
                    projected_d30_union |= keys
            if len(projected_d30_union) > _FULL_D30_APPLICATION_CAP:
                raise D30SubscriptionLimitError(
                    f"Full D30 request would use {len(projected_d30_union)}/"
                    f"{_FULL_D30_APPLICATION_CAP} application slots",
                )

            self._client_d30[session_id] = proposed_d30
            # D30 wins over normal full within a session.
            self._client_full[session_id] = set(full) - proposed_d30
            self._client_ltpc[session_id] = set(ltpc)
            await self._apply()

    async def remove_client(self, session_id: str) -> None:
        """Drops a disconnected session's contribution to the union entirely."""
        async with self._apply_lock:
            self._client_d30.pop(session_id, None)
            self._client_full.pop(session_id, None)
            self._client_ltpc.pop(session_id, None)
            await self._apply()

    async def set_open_position_instruments(self, instrument_keys: set[str]) -> None:
        """Replaces the always-needed open-position set -- called by whatever refreshes
        `PositionPnlTracker` (see that class's own doc comment) each time it does, so a newly
        opened position starts getting live ticks immediately and a closed one stops."""
        async with self._apply_lock:
            self._position_instruments = instrument_keys
            await self._apply()

    async def _apply(self) -> None:
        d30_union: set[str] = set()
        for keys in self._client_d30.values():
            d30_union |= keys

        full_union: set[str] = set(self._tracked_store.load())
        for keys in self._client_full.values():
            full_union |= keys
        full_union -= d30_union

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
        ltpc_union -= full_union | d30_union

        await self._market_feed_client.replace_subscriptions(
            full_d30=sorted(d30_union),
            full=sorted(full_union),
            ltpc=sorted(ltpc_union),
        )

    def debug_snapshot(self) -> dict[str, object]:
        """Read-only view of what's feeding the full/ltpc union -- see `api/routes.py`'s
        `GET /api/debug/feed-status`. Broken down by source so it's visible *why* a given
        instrument is (or isn't) in the union, not just the final merged set."""
        return {
            "tracked_instruments": sorted(self._tracked_store.load()),
            "position_instruments": sorted(self._position_instruments),
            "d30_application_cap": _FULL_D30_APPLICATION_CAP,
            "client_d30_by_session": {
                session_id: sorted(keys) for session_id, keys in self._client_d30.items()
            },
            "client_full_by_session": {
                session_id: sorted(keys) for session_id, keys in self._client_full.items()
            },
            "client_ltpc_by_session": {
                session_id: sorted(keys) for session_id, keys in self._client_ltpc.items()
            },
        }

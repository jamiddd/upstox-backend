from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# A live *tick* feed connected during market hours should never actually go this long without a
# frame -- if it does, the underlying TCP connection is most likely silently half-open (still
# reports "connected" but nothing is arriving), so treat it as dead and reconnect. Mirrors the
# Android client's own MarketFeedClient.kt stale-feed watchdog. Only meaningful for a feed that's
# expected to stream continuously (the market-data feed) -- `UpstoxPortfolioFeedClient` passes
# `stale_after_seconds=None` to disable this instead, since its feed is purely event-driven and can
# go quiet for long, entirely normal stretches with no order/position activity; Upstox's own docs
# describe it sending transport-level ping frames to keep such an idle connection alive, but the
# `websockets` library answers those automatically without ever surfacing them to `recv()`, so this
# watchdog would otherwise force a reconnect loop on a perfectly healthy but idle connection. A
# genuinely dead portfolio-feed connection is still caught -- by the `websockets` library's own
# ping/pong keepalive raising `ConnectionClosed`, not by this watchdog.
_STALE_AFTER_SECONDS = 30.0
_RECONNECT_DELAY_SECONDS = 2.0
# Upstox tokens expire nightly with no refresh flow (see EncryptedTokenStore's own doc comment) --
# retrying every _RECONNECT_DELAY_SECONDS while there's simply no valid token yet would spam the
# authorize endpoint uselessly for hours (e.g. overnight until the next login). Back off to this
# much longer interval specifically for that case.
_AUTH_PENDING_RETRY_SECONDS = 30.0


class UpstoxAuthPendingError(RuntimeError):
    """Raised by an `authorize` callback to mean "no valid Upstox token right now" -- distinct
    from a transient network/API failure so the reconnect loop can back off much longer instead
    of hammering the authorize endpoint every couple of seconds until the user next logs in."""


class UpstoxWebSocketClient:
    """Resilient reconnecting client for one of Upstox's V3 WebSocket feeds (market-data or
    portfolio-stream) -- both share the same one-time-authorize-URL-then-connect lifecycle and the
    same "a control message sent before the handshake completes is dropped, not queued" behavior,
    so this base is shared between `UpstoxMarketFeedClient` and `UpstoxPortfolioFeedClient` rather
    than duplicating the reconnect/generation-guard machinery twice.

    Ported from the Android app's own `MarketFeedClient.kt`, which already solved this exact set
    of problems client-side: a connection-generation counter so a superseded connection's late
    callbacks/tasks can't corrupt the new one's state, always fetching a fresh one-time URL before
    every connect attempt (Upstox's authorize URLs are single-use), and re-sending the full desired
    subscription set right after the handshake completes.
    """

    def __init__(
        self,
        *,
        name: str,
        authorize: Callable[[], Awaitable[str]],
        on_message: Callable[[Any], None],
        desired_subscriptions: Optional[Callable[[], list[dict[str, Any]]]] = None,
        on_state_change: Optional[Callable[[str], None]] = None,
        stale_after_seconds: Optional[float] = _STALE_AFTER_SECONDS,
    ) -> None:
        self._name = name
        self._authorize = authorize
        self._on_message = on_message
        self._desired_subscriptions = desired_subscriptions or (lambda: [])
        self._stale_after_seconds = stale_after_seconds
        # One of "connected" / "disconnected" / "auth_pending" -- lets a caller (e.g. a
        # notification on repeated feed instability) observe this connection's health without
        # polling the `connected` property itself. Best-effort: exceptions from the callback are
        # not caught here deliberately, same as `on_message`, since a broken callback is a bug the
        # caller should see rather than one this class should paper over.
        self._on_state_change = on_state_change or (lambda state: None)
        self._generation = 0
        self._connection: Any = None
        self._task: Optional[asyncio.Task[None]] = None
        self._stopped = False

    @property
    def connected(self) -> bool:
        return self._connection is not None

    def start(self) -> None:
        """Begins the connect/reconnect loop as a background task. Safe to call once; a second
        call is a no-op so callers don't need to track whether they already started it."""
        if self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Permanently stops this client -- cancels the reconnect loop and closes the socket. Not
        the same as a transient reconnect: nothing resumes after this without a fresh `start()`."""
        self._stopped = True
        self._generation += 1
        connection = self._connection
        self._connection = None
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Best-effort send -- silently drops if not currently connected, same posture as the
        Android client's own `sendControlMessage`: a lost subscribe/unsubscribe here is corrected
        the moment the caller's own desired-subscription-set is next resent on reconnect.

        Sent as a BINARY frame (UTF-8-encoded JSON bytes), not TEXT -- Upstox's V3 feed silently
        ignores subscribe/unsubscribe control messages sent as a TEXT frame (no error, the
        message is just never acted on). This is a real bug the Android client already hit and
        fixed once (see `MarketFeedClient.kt`'s FIX HISTORY #2); encoding to bytes here avoids
        repeating it server-side.
        """
        connection = self._connection
        if connection is None:
            return
        try:
            await connection.send(json.dumps(payload).encode("utf-8"))
        except ConnectionClosed:
            pass

    async def force_reconnect(self, reason: str) -> None:
        """Closes the current upstream socket so the normal reconnect loop authorizes a fresh
        connection and replays the complete desired subscription state.

        Used for partial-mode stalls where other frames keep the connection-wide watchdog alive:
        changing the generation alone would not interrupt an in-flight ``recv()``, so the socket
        is explicitly closed as well.
        """
        self._generation += 1
        connection = self._connection
        self._connection = None
        logger.warning("%s: forcing upstream reconnect (%s)", self._name, reason)
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

    async def _run(self) -> None:
        while not self._stopped:
            try:
                auth_pending = await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("%s: connection loop failed unexpectedly", self._name, exc_info=True)
                auth_pending = False
            if self._stopped:
                return
            self._on_state_change("auth_pending" if auth_pending else "disconnected")
            delay = _AUTH_PENDING_RETRY_SECONDS if auth_pending else _RECONNECT_DELAY_SECONDS
            await asyncio.sleep(delay)

    async def _connect_once(self) -> bool:
        """Returns True if this attempt failed specifically because no valid Upstox token is
        available right now (see `UpstoxAuthPendingError`), so `_run` can back off longer."""
        generation = self._generation + 1
        self._generation = generation

        try:
            url = await self._authorize()
        except UpstoxAuthPendingError:
            return True
        except Exception:
            logger.warning("%s: could not authorize connection", self._name, exc_info=True)
            return False
        # A stop() or a newer _connect_once may have happened while authorization was in flight.
        if generation != self._generation:
            return False

        try:
            async with websockets.connect(url, max_size=None) as connection:
                if generation != self._generation:
                    return False
                self._connection = connection
                logger.info("%s: connected", self._name)
                self._on_state_change("connected")

                for message in self._desired_subscriptions():
                    await self.send_json(message)

                while generation == self._generation:
                    if self._stale_after_seconds is None:
                        message = await connection.recv()
                    else:
                        try:
                            message = await asyncio.wait_for(
                                connection.recv(), timeout=self._stale_after_seconds,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "%s: no frames for %.0fs, reconnecting",
                                self._name, self._stale_after_seconds,
                            )
                            return False
                    self._on_message(message)
        except ConnectionClosed:
            logger.info("%s: connection closed", self._name)
        finally:
            if generation == self._generation:
                self._connection = None
        return False

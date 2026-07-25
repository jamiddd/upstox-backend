from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.services.feed_subscription_manager import FeedSubscriptionManager
from app.services.notification_service import NotificationService
from app.services.oi_snapshot_store import OISnapshotStore
from app.services.signal_snapshot_store import SignalSnapshotStore
from app.services.token_store import EncryptedTokenStore
from app.services.underlying_signals_service import UnderlyingSignalsService
from app.services.upstox_market_feed_client import FeedTick
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

# Matches the app's old REST poll cadence for this same data (see MainViewModel
# .UNDERLYING_SIGNALS_POLL_INTERVAL_MILLIS) -- delivery moves from poll to push, but the
# computation itself and its refresh rate are unchanged for now. Rewiring UnderlyingSignalsService
# itself to be tick-driven (rather than each push re-running the existing REST-backed
# computation) is a separate, larger piece of work, deliberately deferred.
SIGNALS_PUSH_INTERVAL_SECONDS = 60.0

# How many consecutive signals-push failures in a row before this is worth surfacing as a
# notification -- a single blip (e.g. one slow Upstox response) isn't actionable.
_SIGNALS_FAILURE_NOTIFY_THRESHOLD = 3


class ClientSession:
    """One connected app session's own subscription/viewing state -- a session id is just the
    WebSocket's own object id, which is unique for the life of one connection; this app has no
    concept of a persistent user identity beyond the single shared Upstox token."""

    def __init__(self, session_id: str, websocket: WebSocket) -> None:
        self.session_id = session_id
        self.websocket = websocket
        self.full_keys: set[str] = set()
        self.ltpc_keys: set[str] = set()
        self.underlying_key: Optional[str] = None
        self.expiry_date: Optional[str] = None
        self.underlying_symbol: Optional[str] = None
        self.signals_task: Optional[asyncio.Task[None]] = None
        self.consecutive_signal_failures = 0
        self.signals_failure_notified = False

    async def send(self, message: dict[str, Any]) -> None:
        if self.websocket.application_state != WebSocketState.CONNECTED:
            logger.info(
                "Dropping send to session %s: application_state=%s (type=%s)",
                self.session_id, self.websocket.application_state, message.get("type"),
            )
            return
        try:
            await self.websocket.send_text(json.dumps(message))
        except Exception:
            logger.warning("Failed to send to session %s", self.session_id, exc_info=True)


class StreamConnectionManager:
    """Owns every connected app session for the backend's single client-facing WebSocket --
    replaces the app's old direct Upstox connection and its 60s signals / 5s order-status REST
    polls. Fans out live ticks and order-update events to whichever sessions want them, and
    forwards each session's own subscribe/set_underlying requests into the shared
    `FeedSubscriptionManager` (see that class's own doc comment for why subscriptions are unioned
    across sessions rather than replaced by the latest one -- the backend's single Upstox
    connection must keep serving every still-connected session's needs, not just the newest).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        subscription_manager: FeedSubscriptionManager,
        notification_service: Optional[NotificationService] = None,
    ) -> None:
        self._settings = settings
        self._subscription_manager = subscription_manager
        self._notification_service = notification_service
        self._sessions: dict[str, ClientSession] = {}

    async def connect(self, websocket: WebSocket) -> ClientSession:
        """Registers a new session. The caller (`stream_routes.stream_endpoint`) is responsible
        for having already called `websocket.accept()` -- the handshake must be accepted before
        the API-key check so an auth rejection can deliver a real WebSocket close code instead of
        a bare HTTP 403 (see that route's own doc comment)."""
        session = ClientSession(str(id(websocket)), websocket)
        self._sessions[session.session_id] = session
        return session

    async def disconnect(self, session: ClientSession) -> None:
        self._sessions.pop(session.session_id, None)
        if session.signals_task is not None:
            session.signals_task.cancel()
        await self._subscription_manager.remove_client(session.session_id)

    async def handle_message(self, session: ClientSession, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Ignoring malformed stream message from session %s", session.session_id)
            return
        if not isinstance(message, dict):
            return

        message_type = message.get("type")
        logger.info("Stream message from session %s: type=%s", session.session_id, message_type)
        if message_type == "subscribe":
            await self._handle_subscribe(session, message)
        elif message_type == "set_underlying":
            self._handle_set_underlying(session, message)
        # Unknown message types are ignored rather than closing the connection -- forward
        # compatible with a future client sending a message type this backend doesn't know yet.

    async def _handle_subscribe(self, session: ClientSession, message: dict[str, Any]) -> None:
        full = [key for key in message.get("full", []) if isinstance(key, str)]
        ltpc = [key for key in message.get("ltpc", []) if isinstance(key, str)]
        session.full_keys = set(full)
        session.ltpc_keys = set(ltpc)
        await self._subscription_manager.set_client_subscription(session.session_id, full=full, ltpc=ltpc)

    def _handle_set_underlying(self, session: ClientSession, message: dict[str, Any]) -> None:
        underlying_key = message.get("underlying_key")
        if not isinstance(underlying_key, str) or not underlying_key:
            logger.warning(
                "set_underlying message missing/invalid underlying_key for session %s: %r",
                session.session_id, message,
            )
            return
        logger.info(
            "set_underlying for session %s: underlying_key=%s expiry_date=%s",
            session.session_id, underlying_key, message.get("expiry_date"),
        )
        session.underlying_key = underlying_key
        expiry_date = message.get("expiry_date")
        session.expiry_date = expiry_date if isinstance(expiry_date, str) else None
        underlying_symbol = message.get("underlying_symbol")
        session.underlying_symbol = underlying_symbol if isinstance(underlying_symbol, str) else None

        if session.signals_task is not None:
            session.signals_task.cancel()
        session.signals_task = asyncio.create_task(self._push_signals_loop(session))

    async def _push_signals_loop(self, session: ClientSession) -> None:
        while True:
            try:
                await self._push_signals_once(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Signals push failed for session %s", session.session_id, exc_info=True,
                )
            await asyncio.sleep(SIGNALS_PUSH_INTERVAL_SECONDS)

    async def _push_signals_once(self, session: ClientSession) -> None:
        if session.underlying_key is None:
            return
        token_store = EncryptedTokenStore(self._settings)
        try:
            access_token = token_store.load_access_token()
        except (TokenStoreError, UpstoxAuthRequiredError):
            return

        signals_service = UnderlyingSignalsService(
            UpstoxService(self._settings),
            snapshot_store=SignalSnapshotStore(self._settings),
            oi_snapshot_store=OISnapshotStore(self._settings),
        )
        try:
            payload = await signals_service.get_signals(
                access_token,
                underlying_key=session.underlying_key,
                expiry_date=session.expiry_date,
                underlying_symbol=session.underlying_symbol,
            )
        except UpstoxApiError:
            logger.warning(
                "Signals computation failed for session %s", session.session_id, exc_info=True,
            )
            session.consecutive_signal_failures += 1
            if (
                session.consecutive_signal_failures >= _SIGNALS_FAILURE_NOTIFY_THRESHOLD
                and not session.signals_failure_notified
                and self._notification_service is not None
            ):
                session.signals_failure_notified = True
                await self._notification_service.record(
                    category="system",
                    severity="warning",
                    title="Signals computation repeatedly failing",
                    message=(
                        f"Underlying signals have failed {session.consecutive_signal_failures} "
                        "times in a row for a connected session."
                    ),
                )
            return
        session.consecutive_signal_failures = 0
        session.signals_failure_notified = False
        await session.send({"type": "signals", "data": payload})

    async def dispatch_tick(self, tick: FeedTick) -> None:
        for session in list(self._sessions.values()):
            if tick.instrument_key not in session.full_keys and tick.instrument_key not in session.ltpc_keys:
                continue
            candle = tick.one_minute_candle
            await session.send({
                "type": "tick",
                "data": {
                    "instrument_key": tick.instrument_key,
                    "ltp": tick.ltp,
                    "last_trade_time_millis": tick.last_trade_time_millis,
                    "bid_price": tick.bid_price,
                    "ask_price": tick.ask_price,
                    "one_minute_candle": (
                        {
                            "timestamp_millis": candle.timestamp_millis,
                            "open": candle.open,
                            "high": candle.high,
                            "low": candle.low,
                            "close": candle.close,
                            "volume": candle.volume,
                        }
                        if candle is not None
                        else None
                    ),
                },
            })

    async def dispatch_order_update(self, payload: dict[str, Any]) -> None:
        # Order updates aren't scoped to a chart/instrument subscription the way ticks are --
        # every connected session needs to know about a fill regardless of what it's currently
        # watching (matches the old client-side behavior: the 5s order poll ran for the whole app
        # session's lifetime, not just while a chart happened to be open).
        for session in list(self._sessions.values()):
            await session.send({"type": "order_update", "data": payload})

    async def dispatch_notification(self, notification: dict[str, Any]) -> None:
        # Unfiltered, same reasoning as dispatch_order_update -- every connected session sees
        # every notification live; the in-app screen's severity/category filters are applied
        # client-side over the full set, not by asking the backend to only push some of them.
        for session in list(self._sessions.values()):
            await session.send({"type": "notification", "data": notification})

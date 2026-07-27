from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from app.core.exceptions import TokenStoreError, UpstoxAuthRequiredError
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService
from app.services.upstox_ws_client import UpstoxAuthPendingError, UpstoxWebSocketClient

logger = logging.getLogger(__name__)


class UpstoxPortfolioFeedClient:
    """Owns the backend's single persistent connection to Upstox's Portfolio Stream Feed -- a
    separate WebSocket from the market-data feed (`UpstoxMarketFeedClient`) that pushes order and
    position update events instantly, replacing the client's old 5-second order-book REST poll.

    Order updates start flowing automatically the moment the connection opens -- unlike the
    market-data feed, there is no subscribe/unsubscribe control message for this feed.

    NOTE: Upstox documents this feed's payload as JSON with an `update_type` field ("order" for
    order events) and order fields (order id, status, quantities, etc.), plus an optional
    `checksum` the docs describe as `md5(order_id + time_in_micro + api_secret)` for payload
    integrity verification. This implementation parses defensively (only acting on
    `update_type == "order"` and whatever status/id fields are present) but does **not**
    implement the checksum verification yet -- that should be added and this whole payload shape
    cross-checked against a real captured message before this feed is trusted for anything beyond
    a UI convenience (e.g. before removing any REST-based confirmation path that place-order
    responses already provide).
    """

    def __init__(
        self,
        *,
        upstox: UpstoxService,
        token_store: EncryptedTokenStore,
        on_order_update: Callable[[dict[str, Any]], None],
        on_state_change: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._upstox = upstox
        self._token_store = token_store
        self._on_order_update = on_order_update
        self._client = UpstoxWebSocketClient(
            name="UpstoxPortfolioFeedClient",
            authorize=self._authorize,
            on_message=self._on_message,
            on_state_change=on_state_change,
            # This feed is purely event-driven (no periodic ticks), so the base class's
            # tick-feed-oriented staleness watchdog doesn't apply here -- see
            # UpstoxWebSocketClient's own doc comment on `_STALE_AFTER_SECONDS`.
            stale_after_seconds=None,
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
            raise UpstoxAuthPendingError("Upstox login is required for the portfolio feed") from exc
        payload = await self._upstox.get_portfolio_feed_authorize(access_token)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("authorized_redirect_uri"), str):
            raise RuntimeError("Unexpected portfolio feed authorization response")
        return data["authorized_redirect_uri"]

    def _on_message(self, message: Any) -> None:
        text = message.decode("utf-8") if isinstance(message, (bytes, bytearray)) else message
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            # See UpstoxMarketFeedClient._on_message's own doc comment on why logging the actual
            # content (not just that a failure happened) matters for diagnosing a real Upstox-side
            # rejection/error frame instead of it being silently invisible.
            logger.warning(
                "Failed to parse portfolio feed message: %r", str(text)[:2000], exc_info=True,
            )
            return
        if not isinstance(payload, dict):
            return
        if payload.get("update_type") == "order":
            self._on_order_update(payload)

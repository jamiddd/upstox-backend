from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.config import Settings
from app.services.device_token_store import DeviceTokenStore
from app.services.notification_store import SEVERITIES, NotificationStore

logger = logging.getLogger(__name__)

# Which severities each push preference actually pushes for -- "off" pushes nothing, "critical"
# only the highest tier, "everything" all three. Mirrors DeviceTokenStore's own preference values.
_PUSH_SEVERITIES = {
    "off": frozenset(),
    "critical": frozenset({"critical"}),
    "everything": frozenset(SEVERITIES),
}


class NotificationService:
    """The single place every notification-worthy event across this backend goes through (see
    `docs/MAIN_SCREEN_API.md`'s Notifications section for the full list of scenarios this backs).

    Persists to `NotificationStore`, live-dispatches to any connected app session over the
    existing `GET /api/stream` channel (best-effort -- a session catches up via REST if it missed
    the push), and -- once `fcm_service` is wired in (Phase 4) -- sends a system-tray push if the
    registered device's own preference wants this severity.

    Constructed fresh and cheaply from `Settings` wherever it's needed (matching this backend's
    existing "construct per use, not a shared singleton" posture for its other small stores), but
    `stream_manager`/`fcm_service` are optional injected dependencies since not every caller has
    them on hand: background tasks running in `app.main`'s lifespan construct one instance with the
    already-in-scope `stream_manager`; HTTP routes get one via `Depends` reading it off
    `request.app.state`.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        stream_manager: Optional[Any] = None,
        fcm_service: Optional[Any] = None,
    ) -> None:
        self.store = NotificationStore(settings)
        self.device_token_store = DeviceTokenStore(settings)
        self.stream_manager = stream_manager
        self.fcm_service = fcm_service

    async def record(
        self,
        *,
        category: str,
        severity: str,
        title: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        notification = self.store.record(
            category=category, severity=severity, title=title, message=message, details=details,
        )

        if self.stream_manager is not None:
            try:
                await self.stream_manager.dispatch_notification(notification)
            except Exception:
                # Best-effort, same posture as every other live-dispatch in this backend -- the
                # notification is already durably persisted; a connected app just needs to poll
                # REST to catch up if this particular push is lost.
                logger.warning("Failed to dispatch notification over the stream", exc_info=True)

        if self.fcm_service is not None:
            await self._maybe_push(notification)

        return notification

    async def _maybe_push(self, notification: dict[str, Any]) -> None:
        token, preference = self.device_token_store.load()
        if not token:
            return
        if notification["severity"] not in _PUSH_SEVERITIES.get(preference, frozenset()):
            return
        try:
            await self.fcm_service.send(
                token,
                title=notification["title"],
                body=notification["message"],
                data={"notification_id": str(notification["id"]), "category": notification["category"]},
            )
        except Exception:
            # A push failing must never take down whatever real event triggered this notification
            # -- it's already persisted and stream-dispatched regardless.
            logger.warning("Failed to send FCM push for notification %s", notification["id"], exc_info=True)

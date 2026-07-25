from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, messaging

from app.core.config import Settings

logger = logging.getLogger(__name__)

# A named (not default) Firebase app -- constructing this more than once (e.g. once per test, or
# if a future caller builds a second FcmService) would otherwise raise on firebase_admin's shared
# default app registry. Any name works; this one just makes it obvious in a debugger/log which
# app a given firebase_admin call is running against.
_APP_NAME = "personalscalper"


class FcmService:
    """Sends system-tray push notifications through Firebase Cloud Messaging.

    Constructed once in `app.main`'s lifespan and handed to `NotificationService` as its optional
    `fcm_service` dependency (see that class's `_maybe_push`, which already implements every bit of
    push-preference gating and exception handling this needs -- `send` here only has to do the
    actual Firebase call).

    If `settings.firebase_service_account_path` doesn't exist, this stays permanently unconfigured
    and `send` is a no-op -- this backend must keep working (in-app notification log + live stream)
    with zero Firebase setup, exactly as it did through Phases 1-3.
    """

    def __init__(self, settings: Settings) -> None:
        self._app: firebase_admin.App | None = None
        path = Path(settings.firebase_service_account_path)
        if not path.exists():
            logger.info("Firebase service account not found at %s; push notifications disabled", path)
            return
        cred = credentials.Certificate(str(path))
        try:
            self._app = firebase_admin.get_app(name=_APP_NAME)
        except ValueError:
            self._app = firebase_admin.initialize_app(cred, name=_APP_NAME)

    async def send(
        self,
        token: str,
        *,
        title: str,
        body: str,
        data: dict[str, str],
    ) -> None:
        """Sends a data-only FCM message -- deliberately omits the `notification=` field so
        Android's `FirebaseMessagingService.onMessageReceived` fires in every app state (Firebase
        would otherwise auto-render a plain system notification, bypassing the app's own
        severity-based channel/styling, whenever the app is backgrounded)."""
        if self._app is None:
            return
        message = messaging.Message(
            # `Message.fid` is a distinct addressing scheme (Firebase Installations), not an
            # alias for a classic FirebaseMessaging.getToken() registration token -- passing our
            # token there made every send fail as UnregisteredError regardless of how fresh the
            # token was. `token` is deprecated in this SDK version but is still the correct field
            # for this token type.
            token=token,
            data={"title": title, "body": body, **data},
        )
        await asyncio.to_thread(messaging.send, message, app=self._app)

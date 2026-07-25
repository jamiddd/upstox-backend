from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxAuthRequiredError
from app.services.notification_service import NotificationService
from app.services.token_store import EncryptedTokenStore

logger = logging.getLogger(__name__)

_POLL_SECONDS = 60.0


async def run_auth_watchdog(settings: Settings, notification_service: NotificationService) -> None:
    """Records a notification the moment the stored Upstox token stops being usable -- there is
    no single request choke point worth hooking for this any more (every protected route already
    converts the same failure into its own 401), so this polls independently, matching this
    backend's other background pollers. Only notifies on the *transition* into an unusable token,
    not on every tick, so it fires once per actual login expiry rather than spamming forever.
    """
    token_store = EncryptedTokenStore(settings)
    was_authenticated: Optional[bool] = None

    while True:
        try:
            is_authenticated = _check_authenticated(token_store)
            if was_authenticated is True and is_authenticated is False:
                await notification_service.record(
                    category="auth",
                    severity="critical",
                    title="Upstox login expired",
                    message="Re-login to resume live trading data and order actions.",
                )
            was_authenticated = is_authenticated
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auth watchdog tick failed unexpectedly")
        await asyncio.sleep(_POLL_SECONDS)


def _check_authenticated(token_store: EncryptedTokenStore) -> bool:
    if not token_store.has_token():
        return False
    try:
        token_store.load_access_token()
        return True
    except (TokenStoreError, UpstoxAuthRequiredError):
        return False

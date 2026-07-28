from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.exceptions import UpstoxAutoLoginError
from app.services.auto_login_state_store import AutoLoginAttemptState, AutoLoginStateStore
from app.services.journal_reconciler import JournalReconciler
from app.services.notification_service import NotificationService
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService
from app.services.upstox_totp_login import UpstoxTotpLoginService

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_POLL_SECONDS = 30.0
# Just after Upstox's funds-endpoint nightly maintenance window ends (~5:30 AM IST, per
# app/services/main_screen_service.py's own doc comment on that window) and well before the
# 9:15 AM market open.
_ATTEMPT_FROM_HOUR = 5
_ATTEMPT_FROM_MINUTE = 35
# Repeatedly POSTing failed logins against Upstox's real servers risks looking like
# credential-stuffing/account-lockout triggers -- this is deliberately NOT a "retry forever" loop
# like some of this backend's other pollers. After this many failed attempts in one day, stop and
# wait for tomorrow; the existing manual OAuth flow (and auth_watchdog's notification) remain the
# fallback.
_MAX_ATTEMPTS_PER_DAY = 5


async def run_auto_login_scheduler(
    settings: Settings,
    notification_service: NotificationService,
    journal_reconciler: JournalReconciler,
) -> None:
    """Logs the backend back in to Upstox every morning by itself, using the account's TOTP
    secret + transaction PIN (see `UpstoxTotpLoginService`), so a human never has to click through
    the browser OAuth flow after the nightly ~3:30 AM IST token expiry. Additive, not a
    replacement -- the manual `/auth/callback` flow keeps working exactly as before if this ever
    fails or Upstox changes something that breaks the reverse-engineered login sequence.
    """
    state_store = AutoLoginStateStore(settings)
    token_store = EncryptedTokenStore(settings)
    login_service = UpstoxTotpLoginService(settings, UpstoxService(settings))

    while True:
        try:
            now = datetime.now(_IST)
            today = now.date().isoformat()
            state = state_store.load()
            already_attempted_today = state is not None and state.date == today
            attempts_so_far = state.attempt_count if already_attempted_today else 0
            already_succeeded_today = already_attempted_today and state.succeeded

            past_attempt_hour = (now.hour, now.minute) >= (_ATTEMPT_FROM_HOUR, _ATTEMPT_FROM_MINUTE)
            # Also attempts immediately on startup if there's no valid token at all yet -- covers
            # redeploys/restarts happening later in the day, not just the scheduled morning window.
            should_attempt = not already_succeeded_today and attempts_so_far < _MAX_ATTEMPTS_PER_DAY and (
                past_attempt_hour or not token_store.has_token()
            )

            if should_attempt:
                try:
                    token_payload = await login_service.login()
                    token_store.save(token_payload)
                    state_store.save(AutoLoginAttemptState(today, attempts_so_far + 1, True))
                    logger.info("Automated Upstox login succeeded")
                    asyncio.create_task(journal_reconciler.reconcile())
                    await notification_service.record(
                        category="auth",
                        severity="info",
                        title="Automated login succeeded",
                        message="Upstox re-authenticated automatically -- no action needed.",
                    )
                except UpstoxAutoLoginError:
                    attempts_so_far += 1
                    state_store.save(AutoLoginAttemptState(today, attempts_so_far, False))
                    logger.warning("Automated Upstox login attempt %d/%d failed", attempts_so_far, _MAX_ATTEMPTS_PER_DAY, exc_info=True)
                    if attempts_so_far >= _MAX_ATTEMPTS_PER_DAY:
                        await notification_service.record(
                            category="auth",
                            severity="critical",
                            title="Automated login failed",
                            message=(
                                f"Automated Upstox login failed {_MAX_ATTEMPTS_PER_DAY} times today "
                                "and has stopped retrying. Log in manually from the app."
                            ),
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto-login scheduler tick failed unexpectedly")
        await asyncio.sleep(_POLL_SECONDS)

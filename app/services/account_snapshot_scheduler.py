from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.services.account_snapshot_store import AccountSnapshot, AccountSnapshotStore
from app.services.journal_store import JournalStore
from app.services.main_screen_service import MainScreenService
from app.services.notification_service import NotificationService
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")
_POLL_SECONDS = 30.0


async def run_account_snapshot_scheduler(
    settings: Settings, notification_service: NotificationService,
) -> None:
    """Persist one net account estimate after 23:00 IST, independent of the Android app."""
    store = AccountSnapshotStore(settings)
    service = MainScreenService(UpstoxService(settings), journal_store=JournalStore(settings))
    last_failure_notified_date: Optional[str] = None

    while True:
        try:
            now = datetime.now(_IST)
            existing = await asyncio.to_thread(store.load)
            already_captured = existing is not None and existing.captured_date == now.date().isoformat()
            if now.hour == 23 and not already_captured:
                token_store = EncryptedTokenStore(settings)
                if token_store.has_token():
                    access_token = token_store.load_access_token()
                    summary = await service.summary(access_token)
                    # closing_balance = opening_balance + payin_amount + profit_loss -
                    # todays_charges (see MainScreenService.summary's own doc comment) -- the true
                    # day-end account value, not just whatever Upstox's live margin totals happen
                    # to report at this instant. Previously this preferred available_margin +
                    # margin_used (a raw broker snapshot that doesn't reflect today's P&L/charges
                    # the same way), falling back to closing_balance only when that was <= 0 --
                    # backwards from what should be persisted as "the balance after today".
                    estimate = float(summary["closing_balance"])
                    if estimate > 0 and summary.get("funds_unavailable_note") is None:
                        snapshot = AccountSnapshot(
                            estimated_balance=estimate,
                            captured_at=now.isoformat(),
                        )
                        await asyncio.to_thread(store.save, snapshot)
                        logger.info("Stored 23:00 account estimate %.2f", estimate)
                        await notification_service.record(
                            category="account",
                            severity="info",
                            title="Account snapshot captured",
                            message=f"Estimated balance of {estimate:.2f} recorded for {now.date().isoformat()}.",
                        )
        except asyncio.CancelledError:
            raise
        except (TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError):
            logger.warning("Unable to capture 23:00 account snapshot", exc_info=True)
            today = datetime.now(_IST).date().isoformat()
            if last_failure_notified_date != today:
                last_failure_notified_date = today
                await notification_service.record(
                    category="account",
                    severity="warning",
                    title="Account snapshot failed",
                    message="Could not capture the 23:00 account estimate. Will keep retrying.",
                )
        except Exception:
            logger.exception("Account snapshot scheduler tick failed unexpectedly")
        await asyncio.sleep(_POLL_SECONDS)

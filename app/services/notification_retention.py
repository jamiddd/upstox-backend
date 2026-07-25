from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.core.config import Settings
from app.services.notification_store import NotificationStore

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 60 * 60


async def run_notification_retention(
    settings: Settings,
    *,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Delete notification rows older than the configured retention window once per UTC day."""
    store = NotificationStore(settings)
    clock = now or (lambda: datetime.now(timezone.utc))
    cleaned_for_date = None

    while True:
        try:
            today = clock().astimezone(timezone.utc).date()
            if cleaned_for_date != today:
                cutoff = today - timedelta(days=settings.notification_retention_days)
                deleted = await asyncio.to_thread(store.delete_expired_before, cutoff)
                cleaned_for_date = today
                if deleted:
                    logger.info("Deleted %d expired notifications older than %s", deleted, cutoff)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Notification retention cleanup failed", exc_info=True)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

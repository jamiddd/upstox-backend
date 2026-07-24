from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.services.account_snapshot_store import AccountSnapshot, AccountSnapshotStore
from app.services.main_screen_service import MainScreenService
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")
_POLL_SECONDS = 30.0


async def run_account_snapshot_scheduler(settings: Settings) -> None:
    """Persist one net account estimate after 23:00 IST, independent of the Android app."""
    store = AccountSnapshotStore(settings)
    service = MainScreenService(UpstoxService(settings))

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
                    broker_net = float(summary["available_margin"]) + float(summary["margin_used"])
                    estimate = broker_net if broker_net > 0 else float(summary["closing_balance"])
                    if estimate > 0 and summary.get("funds_unavailable_note") is None:
                        snapshot = AccountSnapshot(
                            estimated_balance=estimate,
                            captured_at=now.isoformat(),
                        )
                        await asyncio.to_thread(store.save, snapshot)
                        logger.info("Stored 23:00 account estimate %.2f", estimate)
        except asyncio.CancelledError:
            raise
        except (TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError):
            logger.warning("Unable to capture 23:00 account snapshot", exc_info=True)
        except Exception:
            logger.exception("Account snapshot scheduler tick failed unexpectedly")
        await asyncio.sleep(_POLL_SECONDS)

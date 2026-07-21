from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.core.market_hours import is_market_open
from app.services.main_screen_service import MainScreenService
from app.services.oi_analysis_service import OIAnalysisService
from app.services.oi_snapshot_store import OISnapshotStore
from app.services.token_store import EncryptedTokenStore
from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_LOOP_INTERVAL_SECONDS = 15.0


async def run_oi_snapshot_collector(settings: Settings) -> None:
    """Persist one OI analytics snapshot per tracked underlying and five-minute market slot.

    The database unique constraint is the final duplicate guard, so restarts and multiple API
    workers are harmless. Expiries are retained through their complete expiry date and removed
    on the first loop tick after midnight IST; a later restart performs the same catch-up cleanup.
    """
    token_store = EncryptedTokenStore(settings)
    tracked_store = TrackedInstrumentsStore(settings)
    upstox = UpstoxService(settings)
    main_screen = MainScreenService(upstox)
    analysis_service = OIAnalysisService(upstox)
    snapshot_store: OISnapshotStore | None = None
    cleanup_completed_for: date | None = None

    while True:
        try:
            if snapshot_store is None:
                snapshot_store = await asyncio.to_thread(OISnapshotStore, settings)
            now = datetime.now(_IST)
            cleanup_completed_for = await _collect_tick(
                now=now,
                token_store=token_store,
                tracked_store=tracked_store,
                main_screen=main_screen,
                analysis_service=analysis_service,
                snapshot_store=snapshot_store,
                cleanup_completed_for=cleanup_completed_for,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OI snapshot collector tick failed unexpectedly")
        await asyncio.sleep(_LOOP_INTERVAL_SECONDS)


async def _collect_tick(
    *,
    now: datetime,
    token_store: EncryptedTokenStore,
    tracked_store: TrackedInstrumentsStore,
    main_screen: MainScreenService,
    analysis_service: OIAnalysisService,
    snapshot_store: OISnapshotStore,
    cleanup_completed_for: date | None,
) -> date | None:
    local_now = now.astimezone(_IST)
    today = local_now.date()

    # This is deliberately outside the market-hours/auth checks. At midnight after an expiry,
    # data is removed even though the market is closed and even if the daily token has expired.
    if cleanup_completed_for != today:
        deleted = await asyncio.to_thread(snapshot_store.delete_expired_before, today)
        cleanup_completed_for = today
        if deleted:
            logger.info("Deleted %d expired OI snapshots before %s", deleted, today)

    slot_start = _market_slot(local_now)
    if slot_start is None or not token_store.has_token():
        return cleanup_completed_for
    try:
        access_token = token_store.load_access_token()
    except TokenStoreError:
        return cleanup_completed_for

    for underlying_key in tracked_store.load():
        try:
            underlying_symbol, expiry_date = await main_screen.resolve_underlying_symbol_and_expiry(
                access_token,
                underlying_key,
            )
            if not expiry_date:
                continue
            if await asyncio.to_thread(
                snapshot_store.has_snapshot,
                underlying_key,
                expiry_date,
                slot_start,
            ):
                continue
            analysis = await analysis_service.get_analysis(
                access_token,
                instrument_key=underlying_key,
                expiry=expiry_date,
                date=today.isoformat(),
                change_interval=1,
                bucket_interval=5,
            )
            resolved_expiry = analysis.get("expiry")
            if not isinstance(resolved_expiry, str) or not resolved_expiry:
                resolved_expiry = expiry_date
            inserted = await asyncio.to_thread(
                snapshot_store.save_snapshot,
                underlying_key=underlying_key,
                underlying_symbol=underlying_symbol,
                expiry_date=resolved_expiry,
                slot_start=slot_start,
                observed_at=local_now,
                analysis=analysis,
            )
            if inserted:
                logger.info(
                    "Stored OI snapshot for %s expiry %s slot %s",
                    underlying_key,
                    resolved_expiry,
                    slot_start.isoformat(),
                )
        except (UpstoxApiError, UpstoxAuthRequiredError):
            logger.warning("OI snapshot collection failed for %s", underlying_key, exc_info=True)
        except Exception:
            # A malformed payload or local write failure for one underlying must not prevent the
            # remaining tracked instruments from being captured in this slot.
            logger.exception("Unable to store OI snapshot for %s", underlying_key)

    return cleanup_completed_for


def _market_slot(now: datetime) -> datetime | None:
    """Return the current wall-clock-aligned five-minute NSE slot, if the market is open."""
    local = now.astimezone(_IST)
    if not is_market_open(local):
        return None
    return local.replace(minute=local.minute - local.minute % 5, second=0, microsecond=0)

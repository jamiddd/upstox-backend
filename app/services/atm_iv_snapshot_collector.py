from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.core.market_hours import is_market_open
from app.services.atm_iv_snapshot_store import AtmIvSnapshotStore
from app.services.main_screen_service import MainScreenService
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_LOOP_INTERVAL_SECONDS = 15.0
_RETENTION_WINDOW = timedelta(days=30)

# Fixed set of major tradeable indices to capture ATM IV for -- unlike OI's collector (driven by
# whatever the user has picked into TrackedInstrumentsStore), the user explicitly asked for these
# seven regardless of what's tracked, since IV percentile is meant to always be available for the
# indices actually traded from the Terminal screen.
#
# BANKEX and NIFTYNEXT50 don't appear anywhere else in this backend (no prior feature needed
# them) -- these two instrument keys were confirmed 2026-08-15 against Upstox's own instrument
# master (complete.json.gz): "BSE_INDEX|BANKEX" (896 listed CE/PE contracts under it) and
# "NSE_INDEX|Nifty Next 50" (1082 listed CE/PE contracts, trading symbol NIFTYNXT50).
_TRACKED_UNDERLYING_KEYS: tuple[str, ...] = (
    "NSE_INDEX|Nifty 50",
    "BSE_INDEX|SENSEX",
    "NSE_INDEX|Nifty Bank",
    "NSE_INDEX|Nifty Fin Service",
    "NSE_INDEX|NIFTY MID SELECT",
    "BSE_INDEX|BANKEX",
    "NSE_INDEX|Nifty Next 50",
)


async def run_atm_iv_snapshot_collector(settings: Settings) -> None:
    """Persist one ATM IV snapshot per tracked index and five-minute market slot -- same shape as
    `run_oi_snapshot_collector`, just a fixed underlying list and a rolling retention window
    instead of "through expiry day" (see `AtmIvSnapshotStore`'s own doc comment for why).
    """
    token_store = EncryptedTokenStore(settings)
    upstox = UpstoxService(settings)
    main_screen = MainScreenService(upstox)
    snapshot_store: AtmIvSnapshotStore | None = None
    cleanup_completed_for: datetime | None = None

    while True:
        try:
            if snapshot_store is None:
                snapshot_store = await asyncio.to_thread(AtmIvSnapshotStore, settings)
            now = datetime.now(_IST)
            cleanup_completed_for = await _collect_tick(
                now=now,
                token_store=token_store,
                main_screen=main_screen,
                snapshot_store=snapshot_store,
                cleanup_completed_for=cleanup_completed_for,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ATM IV snapshot collector tick failed unexpectedly")
        await asyncio.sleep(_LOOP_INTERVAL_SECONDS)


async def _collect_tick(
    *,
    now: datetime,
    token_store: EncryptedTokenStore,
    main_screen: MainScreenService,
    snapshot_store: AtmIvSnapshotStore,
    cleanup_completed_for: datetime | None,
) -> datetime | None:
    local_now = now.astimezone(_IST)
    today = local_now.date()

    # Same "deliberately outside market-hours/auth checks" posture as the OI collector's own
    # cleanup -- once per day, regardless of whether the market is open or the token is valid.
    if cleanup_completed_for is None or cleanup_completed_for.date() != today:
        cutoff = local_now - _RETENTION_WINDOW
        deleted = await asyncio.to_thread(snapshot_store.delete_older_than, cutoff)
        cleanup_completed_for = local_now
        if deleted:
            logger.info("Deleted %d expired ATM IV snapshots older than %s", deleted, cutoff.isoformat())

    slot_start = _market_slot(local_now)
    if slot_start is None or not token_store.has_token():
        return cleanup_completed_for
    try:
        access_token = token_store.load_access_token()
    except TokenStoreError:
        return cleanup_completed_for

    for underlying_key in _TRACKED_UNDERLYING_KEYS:
        try:
            if await asyncio.to_thread(snapshot_store.has_snapshot, underlying_key, slot_start):
                continue
            underlying_symbol, expiry_date = await main_screen.resolve_underlying_symbol_and_expiry(
                access_token,
                underlying_key,
            )
            if not expiry_date:
                continue
            chain = await main_screen.option_chain(
                access_token,
                underlying_key=underlying_key,
                expiry_date=expiry_date,
            )
            spot_price = float(chain.get("underlying_spot_price") or 0.0)
            strikes = chain.get("strikes")
            if spot_price <= 0.0 or not isinstance(strikes, list) or not strikes:
                continue
            atm_strike_row = min(strikes, key=lambda strike: abs(strike["strike_price"] - spot_price))
            call_iv = float((atm_strike_row.get("ce") or {}).get("iv") or 0.0)
            put_iv = float((atm_strike_row.get("pe") or {}).get("iv") or 0.0)
            # The user's own definition -- always the straight average, held constant throughout
            # the day, no single-side fallback when one leg reads 0.0.
            atm_iv = (call_iv + put_iv) / 2.0
            inserted = await asyncio.to_thread(
                snapshot_store.save_snapshot,
                underlying_key=underlying_key,
                underlying_symbol=underlying_symbol,
                expiry_date=expiry_date,
                slot_start=slot_start,
                observed_at=local_now,
                spot_price=spot_price,
                atm_strike=float(atm_strike_row["strike_price"]),
                call_iv=call_iv,
                put_iv=put_iv,
                atm_iv=atm_iv,
            )
            if inserted:
                logger.info(
                    "Stored ATM IV snapshot for %s slot %s: %.2f%%",
                    underlying_key,
                    slot_start.isoformat(),
                    atm_iv,
                )
        except (UpstoxApiError, UpstoxAuthRequiredError):
            logger.warning("ATM IV snapshot collection failed for %s", underlying_key, exc_info=True)
        except Exception:
            # A malformed payload or local write failure for one underlying must not prevent the
            # remaining tracked indices from being captured in this slot.
            logger.exception("Unable to store ATM IV snapshot for %s", underlying_key)

    return cleanup_completed_for


def _market_slot(now: datetime) -> datetime | None:
    """Return the current wall-clock-aligned five-minute NSE slot, if the market is open."""
    local = now.astimezone(_IST)
    if not is_market_open(local):
        return None
    return local.replace(minute=local.minute - local.minute % 5, second=0, microsecond=0)

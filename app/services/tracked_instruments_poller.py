from __future__ import annotations

import asyncio
import logging
from time import monotonic

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.core.market_hours import is_market_open
from app.services.main_screen_service import MainScreenService
from app.services.token_store import EncryptedTokenStore
from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.underlying_signals_service import UnderlyingSignalsService
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

# The loop wakes up this often to check whether anything's due -- deliberately more frequent than
# _MIN_REFRESH_SECONDS itself so a newly-tracked instrument (or the market just opening) doesn't
# have to wait up to 5 minutes for the *loop* to notice, only for its own per-key cooldown.
_LOOP_INTERVAL_SECONDS = 60.0
# Matches _record_and_diff's own 5-minute band -- no point calling more often than that, since a
# closer-together snapshot wouldn't be used for the delta anyway. This is what keeps the call
# volume down to roughly one call per tracked underlying every 5 minutes during market hours.
_MIN_REFRESH_SECONDS = 300.0


async def run_tracked_instruments_poller(settings: Settings) -> None:
    """Background loop (started from `app.main`'s lifespan) that keeps 5-minute-change history
    warm -- see `UnderlyingSignalsService._record_and_diff` -- for every underlying_key the user
    has opted into via Settings (see `TrackedInstrumentsStore`), independent of whether the app
    itself is open and polling.

    Without this, a delta suffix only ever appears once the *app* has been open, polling the same
    underlying/expiry, for 5 continuous minutes -- opening the app fresh (or reopening after a
    while) always starts from zero. This closes that gap for whichever instruments are tracked:
    each tick just calls `UnderlyingSignalsService.get_signals` -- the exact same call a real
    client request makes -- purely for its `_record_and_diff` side effect; the returned payload
    itself is discarded here.

    Runs forever until the task is cancelled (see `app.main`'s lifespan shutdown). Every failure
    (Upstox down, no token yet, a single underlying erroring) is caught and logged so one bad
    tick/instrument never kills the loop -- this is a best-effort background warmer, not something
    any request depends on for correctness.
    """
    token_store = EncryptedTokenStore(settings)
    tracked_store = TrackedInstrumentsStore(settings)
    upstox = UpstoxService(settings)
    main_screen = MainScreenService(upstox)
    signals_service = UnderlyingSignalsService(upstox)
    last_polled: dict[str, float] = {}

    while True:
        await asyncio.sleep(_LOOP_INTERVAL_SECONDS)
        try:
            await _poll_due_instruments(
                token_store=token_store,
                tracked_store=tracked_store,
                main_screen=main_screen,
                signals_service=signals_service,
                last_polled=last_polled,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Belt-and-suspenders -- _poll_due_instruments already catches per-instrument
            # failures, but this guarantees the loop itself never dies from something
            # unanticipated (e.g. a bug in the store/config layer, not just Upstox flakiness).
            logger.exception("Tracked-instruments poller tick failed unexpectedly")


async def _poll_due_instruments(
    *,
    token_store: EncryptedTokenStore,
    tracked_store: TrackedInstrumentsStore,
    main_screen: MainScreenService,
    signals_service: UnderlyingSignalsService,
    last_polled: dict[str, float],
) -> None:
    if not is_market_open():
        return
    if not token_store.has_token():
        return
    try:
        access_token = token_store.load_access_token()
    except TokenStoreError:
        return

    now = monotonic()
    for underlying_key in tracked_store.load():
        due_at = last_polled.get(underlying_key)
        if due_at is not None and now - due_at < _MIN_REFRESH_SECONDS:
            continue
        last_polled[underlying_key] = now
        try:
            underlying_symbol, expiry_date = await main_screen.resolve_underlying_symbol_and_expiry(
                access_token, underlying_key,
            )
            await signals_service.get_signals(
                access_token,
                underlying_key=underlying_key,
                expiry_date=expiry_date,
                underlying_symbol=underlying_symbol,
            )
        except (UpstoxApiError, UpstoxAuthRequiredError):
            logger.warning("Tracked-instrument background poll failed for %s", underlying_key, exc_info=True)

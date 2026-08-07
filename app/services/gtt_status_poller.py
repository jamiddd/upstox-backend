from __future__ import annotations

import asyncio
import logging

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.services.gtt_history_store import GttHistoryStore
from app.services.smart_order_service import SmartOrderService
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)
_POLL_SECONDS = 25.0


async def run_gtt_status_poller(settings: Settings) -> None:
    """Background reconciliation for GttHistoryStore -- the only place Upstox's own (unreliable)
    GTT list endpoint is still consulted, now that this backend has stopped depending on it to
    *discover* orders in the first place (see GttHistoryStore's own doc comment -- place/modify/
    cancel in smart_order_service.py write directly to the store the moment Upstox confirms each
    one, which is the actual fix; this poller only refreshes status afterward).

    Each cycle fetches Upstox's list once and archives it. archive() only ever upserts whatever's
    *positively present* in that response -- it never removes or marks-terminal a row just
    because that row's id happened to be absent from one call. That's deliberate: it's what lets
    a resting order eventually transition to TRIGGERED/COMPLETED/EXPIRED once Upstox actually
    reports it having done so, while never treating a single incomplete/flaky list response as
    "the order is gone" -- Upstox is only ever trusted for a *positive* status confirmation here,
    never for an inference drawn from silence.
    """
    store = GttHistoryStore(settings)
    smart_order_service = SmartOrderService(UpstoxService(settings))
    token_store = EncryptedTokenStore(settings)

    while True:
        try:
            if token_store.has_token():
                access_token = token_store.load_access_token()
                all_orders = await smart_order_service.get_all_gtt_orders(access_token)
                await asyncio.to_thread(store.archive, all_orders)
        except asyncio.CancelledError:
            raise
        except (TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError):
            logger.warning("GTT status poller could not reach Upstox this cycle", exc_info=True)
        except Exception:
            logger.exception("GTT status poller tick failed unexpectedly")
        await asyncio.sleep(_POLL_SECONDS)

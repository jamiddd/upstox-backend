from __future__ import annotations

import asyncio
import logging

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxApiError
from app.core.market_hours import is_market_open
from app.services.order_book_lookup import TERMINAL_ORDER_STATUSES, index_orders_by_id, order_status
from app.services.pending_oco_pairs_store import OcoPair, PendingOcoPairsStore
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

# How often the loop checks the order book against whatever pairs are pending -- fast enough that
# a filled leg's sibling doesn't sit live for long (this is money on the table, not a background
# data warmer like the tracked-instruments poller), but not so fast it hammers Upstox's order-book
# endpoint every tick.
_LOOP_INTERVAL_SECONDS = 10.0


async def run_oco_watcher(settings: Settings) -> None:
    """Background loop (started from `app.main`'s lifespan, same as
    `tracked_instruments_poller.run_tracked_instruments_poller`) that reconciles the plain
    target/stoploss order pairs `SmartOrderService.attach_gtt_exits` places for a position with no
    GTT bracket -- see that method's own doc comment for why those are plain orders instead of a
    GTT MULTIPLE bracket, and therefore carry no OCO (one-cancels-other) guarantee from Upstox
    itself.

    Every tick, for every pair still in [PendingOcoPairsStore]: fetches the current order book,
    and if either leg has reached a terminal status (filled, cancelled, or rejected by Upstox/the
    exchange), cancels the other leg (only if it's still live) and drops the pair. Runs forever
    until the task is cancelled (see `app.main`'s lifespan shutdown). Every failure is caught and
    logged so one bad tick/pair never kills the loop -- same best-effort posture as the
    tracked-instruments poller.
    """
    token_store = EncryptedTokenStore(settings)
    pending_store = PendingOcoPairsStore(settings)
    upstox = UpstoxService(settings)

    while True:
        await asyncio.sleep(_LOOP_INTERVAL_SECONDS)
        try:
            await _reconcile_pending_pairs(
                token_store=token_store,
                pending_store=pending_store,
                upstox=upstox,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OCO watcher tick failed unexpectedly")


async def _reconcile_pending_pairs(
    *,
    token_store: EncryptedTokenStore,
    pending_store: PendingOcoPairsStore,
    upstox: UpstoxService,
) -> None:
    if not is_market_open():
        return
    pairs = pending_store.load()
    if not pairs:
        return
    if not token_store.has_token():
        return
    try:
        access_token = token_store.load_access_token()
    except TokenStoreError:
        return

    try:
        order_book_payload = await upstox.get_order_book(access_token)
    except UpstoxApiError:
        logger.warning("OCO watcher failed to fetch the order book", exc_info=True)
        return

    orders_by_id = index_orders_by_id(order_book_payload)
    resolved: list[OcoPair] = []
    for pair in pairs:
        target_status = order_status(orders_by_id.get(pair.target_order_id))
        stoploss_status = order_status(orders_by_id.get(pair.stoploss_order_id))

        # Neither leg has reached a terminal state yet (or the order book hasn't caught up to a
        # just-placed pair) -- nothing to reconcile this tick.
        if target_status not in TERMINAL_ORDER_STATUSES and stoploss_status not in TERMINAL_ORDER_STATUSES:
            continue

        if target_status == "complete" and stoploss_status not in TERMINAL_ORDER_STATUSES:
            await _cancel_leg(upstox, access_token, pair.stoploss_order_id)
        elif stoploss_status == "complete" and target_status not in TERMINAL_ORDER_STATUSES:
            await _cancel_leg(upstox, access_token, pair.target_order_id)
        resolved.append(pair)

    pending_store.remove(resolved)


async def _cancel_leg(upstox: UpstoxService, access_token: str, order_id: str) -> None:
    try:
        await upstox.cancel_order(access_token, order_id)
    except UpstoxApiError:
        # Best-effort -- the sibling may have already been cancelled manually (e.g. via the app's
        # own Order History "Cancel order" button) between this tick's order-book fetch and now.
        # Either way, the pair itself is still resolved (dropped from pending_store by the caller)
        # since there's nothing further this watcher can do about it.
        logger.warning("OCO watcher failed to cancel sibling order %s", order_id, exc_info=True)

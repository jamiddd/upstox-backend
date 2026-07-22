from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxApiError
from app.core.market_hours import is_market_open
from app.services.order_book_lookup import TERMINAL_ORDER_STATUSES, index_orders_by_id, order_status
from app.services.pending_oco_pairs_store import PendingExit, PendingOcoPairsStore
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

# How often the loop checks pending exits against the order book and live quotes. Deliberately
# tight -- unlike the tracked-instruments poller (a background data warmer with no real urgency),
# a slow tick here directly costs money: the target leg only exists as a price level this loop
# itself watches (see PendingExit's own doc comment for why it can't be a resting broker order),
# so the gap between price crossing the target and this loop noticing is pure, uncontrolled
# slippage. 5s, not 10s -- still nowhere near hammering Upstox given this only ever fetches for
# instruments with something genuinely pending.
_LOOP_INTERVAL_SECONDS = 5.0

# How long to wait after cancelling a resting stoploss order before firing the target's own
# market order -- see _fire_target's own doc comment.
_STOPLOSS_CANCEL_SETTLE_SECONDS = 0.8


async def run_oco_watcher(settings: Settings) -> None:
    """Background loop (started from `app.main`'s lifespan, same as
    `tracked_instruments_poller.run_tracked_instruments_poller`) that watches every
    [PendingExit] `SmartOrderService.attach_gtt_exits` armed for a position with no GTT bracket.

    Each tick, per pending exit:
      - If its stoploss order has filled, the position is already closed by the stop -- just drop
        the pending exit (there was never a live target order to cancel).
      - If its stoploss order was cancelled/rejected (e.g. manually, via the app's own Order
        History "Cancel order" button), the protective leg is gone -- drop the pending exit rather
        than silently keep watching a target with no stop backing it.
      - Otherwise, the stoploss is still live: fetch the instrument's current price and, if it's
        crossed the target level, fire it -- place a real MARKET sell/buy for the target quantity,
        best-effort cancel the now-redundant stoploss order, and drop the pending exit. See
        [_fire_target]'s own doc comment for why the pending exit is dropped *before* placing that
        market order, not after.

    Runs forever until the task is cancelled (see `app.main`'s lifespan shutdown). Every failure is
    caught and logged so one bad tick/pending-exit never kills the loop -- same best-effort posture
    as the tracked-instruments poller.
    """
    token_store = EncryptedTokenStore(settings)
    pending_store = PendingOcoPairsStore(settings)
    upstox = UpstoxService(settings)

    while True:
        await asyncio.sleep(_LOOP_INTERVAL_SECONDS)
        try:
            await _reconcile_pending_exits(
                token_store=token_store,
                pending_store=pending_store,
                upstox=upstox,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OCO watcher tick failed unexpectedly")


async def _reconcile_pending_exits(
    *,
    token_store: EncryptedTokenStore,
    pending_store: PendingOcoPairsStore,
    upstox: UpstoxService,
) -> None:
    if not is_market_open():
        return
    pending_exits = pending_store.load()
    if not pending_exits:
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

    resolved: list[PendingExit] = []
    still_live: list[PendingExit] = []
    for pending_exit in pending_exits:
        status = order_status(orders_by_id.get(pending_exit.stoploss_order_id))
        if status == "complete" or status in TERMINAL_ORDER_STATUSES:
            # Filled (stop hit -- position already closed) or cancelled/rejected (protective leg
            # gone) -- either way nothing left to watch for this one. There was never a live
            # target order, so there's nothing to cancel.
            resolved.append(pending_exit)
        else:
            # None (order book hasn't caught up to a just-placed stoploss yet) or a live/pending
            # status ("open", "trigger pending", etc.) -- keep watching.
            still_live.append(pending_exit)
    if resolved:
        pending_store.remove(resolved)
    if not still_live:
        return

    instrument_keys = sorted({pending_exit.instrument_key for pending_exit in still_live})
    try:
        quotes_payload = await upstox.get_quotes(access_token, ",".join(instrument_keys))
    except UpstoxApiError:
        logger.warning("OCO watcher failed to fetch quotes", exc_info=True)
        return
    prices_by_instrument = _index_last_prices(quotes_payload)

    for pending_exit in still_live:
        ltp = prices_by_instrument.get(pending_exit.instrument_key)
        if ltp is None or not _target_reached(pending_exit, ltp):
            continue
        await _fire_target(upstox, access_token, pending_store, pending_exit)


def _target_reached(pending_exit: PendingExit, ltp: float) -> bool:
    # exit_transaction_type "SELL" closes a long -- profitable once price has risen to/through the
    # target. "BUY" closes a short -- profitable once price has fallen to/through it.
    if pending_exit.exit_transaction_type == "SELL":
        return ltp >= pending_exit.target_trigger_price
    return ltp <= pending_exit.target_trigger_price


async def _fire_target(
    upstox: UpstoxService,
    access_token: str,
    pending_store: PendingOcoPairsStore,
    pending_exit: PendingExit,
) -> None:
    """Cancels [pending_exit]'s still-resting stoploss leg *first*, then drops it from
    [pending_store] right before placing the target's own market order -- in that order, for two
    different reasons that would otherwise fight each other:

    1. The stoploss is a real resting order reserving exit-side quantity/margin. Firing the
       target's market order while it's still live makes Upstox see more pending exposure than
       the position actually holds and reject it for margin (a "naked" excess) -- the exact same
       failure the app's manual/bulk exit paths were fixed for (see
       OrderPlacementViewModel.cancelExistingExitOrdersIfNeeded and
       SmartOrderService._cancel_resting_stoploss_orders). The market order below must never be
       the first exit-side order in flight.
    2. [pending_store] is only dropped right before that market order goes out (not any earlier),
       so a crash between the cancel and the placement leaves this pending_exit still tracked --
       the next tick sees the stoploss now cancelled/terminal and simply resolves/drops it as
       "protective leg gone" (see `_reconcile_pending_exits`), rather than silently losing the
       fact that a target was actually reached. Missing a target fire this way is a lost profit
       opportunity, not a double-sell, so it's the safe side to lean on -- same bias the previous
       version of this function already had, just re-pointed at the new first step.

    If the cancel itself fails (including the stoploss having just filled/gone terminal in the
    race between this tick's order-book snapshot and now), the target isn't fired this tick --
    [pending_exit] stays tracked so the next tick re-evaluates it from scratch, which correctly
    drops it once the order book reflects that terminal status instead of firing a now-wrong
    market order on top of it.
    """
    try:
        await upstox.cancel_order(access_token, pending_exit.stoploss_order_id)
    except UpstoxApiError:
        logger.warning(
            "OCO watcher failed to cancel stoploss %s before firing target for %s -- retrying next tick",
            pending_exit.stoploss_order_id,
            pending_exit.instrument_key,
            exc_info=True,
        )
        return

    # Gives Upstox a moment to actually release the quantity/margin the cancelled stoploss was
    # holding before the market order below asks for it -- same settle delay as the app's own
    # cancel-then-exit fixes, for the same reason (a cancel-then-immediately-place sequence can
    # still race and hit the same rejection the cancel was meant to prevent).
    await asyncio.sleep(_STOPLOSS_CANCEL_SETTLE_SECONDS)

    pending_store.remove([pending_exit])
    try:
        await upstox.place_order(
            access_token,
            instrument_key=pending_exit.instrument_key,
            transaction_type=pending_exit.exit_transaction_type,
            quantity=pending_exit.quantity,
            product=pending_exit.product,
            order_type="MARKET",
        )
    except UpstoxApiError:
        # The stoploss is already cancelled and the pending exit already dropped -- this position
        # is genuinely unprotected until a human notices (Order History/Main still show it as an
        # open position with no S/L, same as any other position with no bracket). Not retried
        # automatically: retrying a possibly-already-placed order blindly risks a real double-sell.
        logger.warning(
            "OCO watcher failed to fire target MARKET order for %s", pending_exit.instrument_key, exc_info=True
        )


def _index_last_prices(quotes_payload: dict[str, Any]) -> dict[str, float]:
    """Maps each instrument_key present in a `get_quotes` response to its last_price -- same
    "direct key match, else scan for instrument_token" resilience as
    `main_screen_service._find_quote` (Upstox's quotes response is keyed by its own
    "EXCHANGE:SYMBOL" form, not the instrument_key used in the request).
    """
    data = quotes_payload.get("data")
    if not isinstance(data, dict):
        return {}
    prices: dict[str, float] = {}
    for quote in data.values():
        if not isinstance(quote, dict):
            continue
        instrument_key = quote.get("instrument_token")
        last_price = quote.get("last_price")
        if isinstance(instrument_key, str) and isinstance(last_price, (int, float)):
            prices[instrument_key] = float(last_price)
    return prices

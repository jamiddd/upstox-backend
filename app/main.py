from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.routes import router as api_router
from app.api.stream_routes import router as stream_router
from app.core.config import get_settings
from app.services.candle_cache_store import CandleCacheStore
from app.services.feed_subscription_manager import FeedSubscriptionManager
from app.services.live_candle_builder import LiveCandleBuilder, feed_candle_to_cache_row
from app.services.oco_watcher import run_oco_watcher
from app.services.account_snapshot_scheduler import run_account_snapshot_scheduler
from app.services.oi_snapshot_collector import run_oi_snapshot_collector
from app.services.order_fill_detector import OrderFillDetector
from app.services.stream_connection_manager import StreamConnectionManager
from app.services.token_store import EncryptedTokenStore
from app.services.tracked_instruments_poller import run_tracked_instruments_poller
from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.upstox_market_feed_client import FeedTick, UpstoxMarketFeedClient
from app.services.upstox_portfolio_feed_client import UpstoxPortfolioFeedClient
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

# How often the tracked-instruments "always needed" subscription set is re-applied -- matches
# run_tracked_instruments_poller's own loop cadence, since a Settings change to the tracked list
# isn't otherwise pushed to the feed subscription manager immediately.
_SUBSCRIPTION_REFRESH_INTERVAL_SECONDS = 60.0


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # See TrackedInstrumentsStore / run_tracked_instruments_poller's own doc comment for why this
    # exists -- keeps 5-minute-change history warm for Settings-picked underlyings even while no
    # client is actively polling. Cancelled cleanly on shutdown, same as any other background task
    # tied to the app's own lifetime.
    poller_task = asyncio.create_task(run_tracked_instruments_poller(settings))
    # See PendingOcoPairsStore / run_oco_watcher's own doc comment -- reconciles the plain
    # target/stoploss order pairs SmartOrderService.attach_gtt_exits places for a position with
    # no GTT bracket, independent of whether the app itself is open. Same lifecycle as the poller
    # above.
    oco_watcher_task = asyncio.create_task(run_oco_watcher(settings))
    oi_collector_task = asyncio.create_task(run_oi_snapshot_collector(settings))
    account_snapshot_task = asyncio.create_task(run_account_snapshot_scheduler(settings))

    # The backend's own persistent Upstox connections, replacing the client's direct-to-Upstox
    # WebSocket and the old REST-polling paths for live prices/candles/order status. Stored on
    # app.state so a later client-facing WS endpoint can reach them without another construction.
    #
    # CandleCacheStore's constructor creates its directory/table eagerly (unlike EncryptedTokenStore
    # /TrackedInstrumentsStore, which only touch the filesystem lazily on save()) -- constructing
    # it directly in the lifespan body, unguarded, would mean a filesystem hiccup here takes down
    # the *entire* app's startup, unlike every background poller's own construction of the same
    # store, which already fails in isolation (see run_tracked_instruments_poller's own try/except
    # posture). Guarded the same way here: the live feed's candle cache is a nice-to-have on top of
    # the feed itself, not something the rest of the app should refuse to start without.
    try:
        candle_cache_store: Optional[CandleCacheStore] = CandleCacheStore(settings)
    except Exception:
        logger.warning("Could not initialize the live candle cache store", exc_info=True)
        candle_cache_store = None
    candle_builder = LiveCandleBuilder(
        on_candle_completed=lambda instrument_key, candle: _persist_completed_candle(
            candle_cache_store, instrument_key, candle,
        ),
    )
    order_fill_detector = OrderFillDetector()

    market_feed_token_store = EncryptedTokenStore(settings)
    market_feed_upstox = UpstoxService(settings)
    market_feed_client = UpstoxMarketFeedClient(
        upstox=market_feed_upstox,
        token_store=market_feed_token_store,
        on_tick=lambda tick: _on_market_tick(candle_builder, stream_manager, tick),
    )
    portfolio_feed_token_store = EncryptedTokenStore(settings)
    portfolio_feed_upstox = UpstoxService(settings)
    portfolio_feed_client = UpstoxPortfolioFeedClient(
        upstox=portfolio_feed_upstox,
        token_store=portfolio_feed_token_store,
        on_order_update=lambda payload: _on_portfolio_update(order_fill_detector, stream_manager, payload),
    )

    tracked_store = TrackedInstrumentsStore(settings)
    subscription_manager = FeedSubscriptionManager(
        market_feed_client=market_feed_client, tracked_store=tracked_store,
    )
    stream_manager = StreamConnectionManager(settings=settings, subscription_manager=subscription_manager)

    app.state.market_feed_client = market_feed_client
    app.state.portfolio_feed_client = portfolio_feed_client
    app.state.feed_subscription_manager = subscription_manager
    app.state.live_candle_builder = candle_builder
    app.state.stream_manager = stream_manager

    market_feed_client.start()
    portfolio_feed_client.start()
    subscription_refresh_task = asyncio.create_task(
        _run_subscription_refresh(subscription_manager),
    )

    try:
        yield
    finally:
        poller_task.cancel()
        oco_watcher_task.cancel()
        oi_collector_task.cancel()
        account_snapshot_task.cancel()
        subscription_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller_task
        with contextlib.suppress(asyncio.CancelledError):
            await oco_watcher_task
        with contextlib.suppress(asyncio.CancelledError):
            await oi_collector_task
        with contextlib.suppress(asyncio.CancelledError):
            await account_snapshot_task
        with contextlib.suppress(asyncio.CancelledError):
            await subscription_refresh_task
        await market_feed_client.stop()
        await portfolio_feed_client.stop()


async def _run_subscription_refresh(subscription_manager: FeedSubscriptionManager) -> None:
    """Keeps the tracked-instruments "always needed" set applied even if it changes via Settings
    between refreshes -- best-effort background loop, same posture as this backend's other
    pollers (a single failed tick is logged and never kills the loop)."""
    while True:
        await asyncio.sleep(_SUBSCRIPTION_REFRESH_INTERVAL_SECONDS)
        try:
            await subscription_manager.refresh_tracked_instruments()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Feed subscription refresh failed unexpectedly", exc_info=True)


def _persist_completed_candle(
    cache_store: Optional[CandleCacheStore],
    instrument_key: str,
    candle: Any,
) -> None:
    if cache_store is None:
        return
    try:
        cache_store.save(instrument_key, "minutes", 1, [feed_candle_to_cache_row(candle)])
    except Exception:
        # Best-effort cache write -- a failure here must not take down the live feed's tick
        # dispatch, same "background warmer, not a request dependency" posture as the pollers.
        logger.warning("Failed to persist live-built candle for %s", instrument_key, exc_info=True)


def _handle_order_update(detector: OrderFillDetector, payload: dict[str, Any]) -> bool:
    order_id = payload.get("order_id") or payload.get("exchange_order_id")
    status = payload.get("status")
    if not isinstance(order_id, str) or status != "complete":
        return False
    # observe() needs the *current full set* of today's completed order IDs to detect a genuinely
    # new one, but the portfolio feed only ever gives us one order at a time per event -- so this
    # single-ID observation just reports whether *this* id is new, which is exactly what a single
    # order-update event needs (unlike the batched REST-poll case OrderFillDetector was originally
    # designed for, there's no multi-ID-per-tick batching to fold together here).
    is_new_fill = detector.observe([order_id])
    if is_new_fill:
        logger.info("Order %s newly complete", order_id)
    return is_new_fill


def _on_market_tick(
    candle_builder: LiveCandleBuilder,
    stream_manager: StreamConnectionManager,
    tick: FeedTick,
) -> None:
    candle_builder.handle_tick(tick)
    asyncio.create_task(stream_manager.dispatch_tick(tick))


def _on_portfolio_update(
    detector: OrderFillDetector,
    stream_manager: StreamConnectionManager,
    payload: dict[str, Any],
) -> None:
    _handle_order_update(detector, payload)
    # Every order update (not just a newly-detected fill) is forwarded -- the app needs to see
    # a bracket move from "open" to "cancelled"/"rejected" too, not only "complete" transitions;
    # OrderFillDetector's own edge-detection is specifically about when to play the fill sound,
    # a client-side concern, not a filter on what state changes reach the app at all.
    asyncio.create_task(stream_manager.dispatch_order_update(payload))


app = FastAPI(title="Upstox Scalper Backend", version="0.1.0", lifespan=_lifespan)
app.include_router(api_router, prefix="/api")
app.include_router(stream_router, prefix="/api")


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return a simple health response for deployment checks."""
    return {"status": "ok"}


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """Return API error payloads without FastAPI's default detail wrapper."""
    content: Any = exc.detail
    if not isinstance(content, dict):
        content = {"status": "error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

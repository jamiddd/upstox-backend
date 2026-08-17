from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router as api_router
from app.api.stream_routes import router as stream_router
from app.api.order_engine_routes import router as order_engine_router
from app.api.journal_v2_routes import router as journal_v2_router
from app.core.config import get_settings
from app.core.exceptions import TokenStoreError, UpstoxAuthRequiredError
from app.core.market_hours import is_market_open
from app.services.auth_watchdog import run_auth_watchdog
from app.services.auth_watchdog import run_auth_watchdog                                                      
from app.services.auto_login_scheduler import run_auto_login_scheduler    
from app.services.candle_cache_store import CandleCacheStore
from app.services.fcm_service import FcmService
from app.services.feed_subscription_manager import FeedSubscriptionManager
from app.services.gtt_status_poller import run_gtt_status_poller
from app.services.instrument_rules_service import InstrumentRulesService
from app.services.journal_store import JournalStore
from app.services.exit_reconciliation_checker import ExitReconciliationChecker
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker
from app.services.order_engine_max_loss_watcher import check_now as check_order_engine_max_loss_now
from app.services.order_engine_max_loss_watcher import run_fallback_loop as run_order_engine_max_loss_watcher_fallback
from app.services.order_engine_order_service import OrderEngineOrderService, derive_order_tag
from app.services.order_engine_trigger_evaluator import check_now as check_order_engine_triggers_now
from app.services.order_engine_trigger_evaluator import run_fallback_loop as run_order_engine_trigger_evaluator_fallback
from app.services.broker_order_lookup import find_order_by_id
from app.services.order_history_recorder import OrderHistoryRecorder
from app.services.journal_reconciler import JournalReconciler, run_journal_reconciler
from app.services.live_candle_builder import LiveCandleBuilder, feed_candle_to_cache_row
from app.services.max_loss_settings_store import MaxLossSettingsStore
from app.services.max_loss_watcher import check_now as check_max_loss_now
from app.services.max_loss_watcher import run_max_loss_watcher_fallback
from app.services.notification_service import NotificationService
from app.services.notification_retention import run_notification_retention
from app.services.account_snapshot_scheduler import run_account_snapshot_scheduler
from app.services.oi_snapshot_collector import run_oi_snapshot_collector
from app.services.atm_iv_snapshot_collector import run_atm_iv_snapshot_collector
from app.services.order_fill_detector import OrderFillDetector
from app.services.order_flow_analyzer import OrderFlowService
from app.services.position_pnl_tracker import PositionPnlTracker
from app.services.smart_order_service import SmartOrderService
from app.services.stream_connection_manager import StreamConnectionManager
from app.services.token_store import EncryptedTokenStore
from app.services.tracked_instruments_poller import run_tracked_instruments_poller
from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.trade_context_service import TradeContextService
from app.services.underlying_signals_service import UnderlyingSignalsService
from app.services.signal_snapshot_store import SignalSnapshotStore
from app.services.oi_snapshot_store import OISnapshotStore
from app.services.upstox_market_feed_client import FeedTick, UpstoxMarketFeedClient
from app.services.upstox_portfolio_feed_client import UpstoxPortfolioFeedClient
from app.services.upstox_service import UpstoxService

# No prior config meant every logger.info/debug call across this whole backend was silently
# dropped -- Python's root logger defaults to WARNING with no handler, so only warning/error
# calls (e.g. the feed-disconnect notifier, FCM push failures) were ever visible in
# `docker compose logs`. This is the one place guaranteed to run before any other module's
# logger.* call, since every route/service is imported above and reachable only through this
# app.
#
# Also attaches a RotatingFileHandler under /data (the same Docker-volume-backed directory every
# other store in this app already persists to -- see Settings' other *_path fields) so a past
# incident's logs survive container restarts/recreates instead of only living in Docker's own log
# buffer. Rotating (not a bare FileHandler) since this is a long-running always-on process with
# frequent logger.info calls -- an unbounded file would eventually fill the volume.
#
# Every other *_path field in Settings is only touched lazily, when whatever store owns it is
# actually used -- this is the first thing in the app to touch the filesystem eagerly at import
# time, which broke local dev/tests (no /data mount outside the container). File logging is a
# nice-to-have, not something worth failing app startup over, so this falls back to stream-only
# logging if the directory can't be created for any reason.
_log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _settings_for_logging = get_settings()
    _settings_for_logging.log_file_path.parent.mkdir(parents=True, exist_ok=True)
    _file_handler = RotatingFileHandler(
        _settings_for_logging.log_file_path, maxBytes=10_000_000, backupCount=5,
    )
    _file_handler.setFormatter(logging.Formatter(_log_format))
    _log_handlers.append(_file_handler)
except OSError:
    pass
logging.basicConfig(
    level=logging.INFO,
    format=_log_format,
    # uvicorn may have already attached its own handler to the root logger by the time this
    # module imports (depends on how it's launched) -- plain basicConfig() is a no-op once any
    # handler exists, force=True guarantees this config wins regardless.
    handlers=_log_handlers,
    force=True,
)

logger = logging.getLogger(__name__)

# How often the tracked-instruments "always needed" subscription set is re-applied -- matches
# run_tracked_instruments_poller's own loop cadence, since a Settings change to the tracked list
# isn't otherwise pushed to the feed subscription manager immediately.
_SUBSCRIPTION_REFRESH_INTERVAL_SECONDS = 60.0

# Fallback-only cadence for PositionPnlTracker's own structural refresh -- _on_portfolio_update
# already triggers an immediate one on every real order/position change; see
# _run_position_tracker_refresh's own doc comment for what this interval actually catches.
_POSITION_TRACKER_REFRESH_INTERVAL_SECONDS = 15.0

# D30 cohort recovery needs a prompt second observation after its explicit mode reset. Individual
# instrument nudges remain protected by their own 45-second age threshold and timestamp reset.
_MARKET_FEED_STALENESS_CHECK_INTERVAL_SECONDS = 10.0
# Per-instrument threshold for UpstoxMarketFeedClient.resend_stale_subscriptions -- deliberately a
# DIFFERENT, independent concern from UpstoxWebSocketClient's own connection-wide
# _STALE_AFTER_SECONDS=30 watchdog. That one detects "no frames at all from the whole connection";
# this one detects "no frames for one specific subscribed instrument while everything else on the
# connection is fine" -- do not conflate the two or import one into the other.
_MARKET_FEED_INSTRUMENT_STALE_AFTER_SECONDS = 45.0
# D30 normally emits depth changes continuously. If the entire D30 cohort is silent while normal
# Full/LTPC modes remain fresh, reset that mode first and then the actual upstream socket.
_D30_COHORT_STALE_AFTER_SECONDS = 20.0
_D30_COHORT_RECONNECT_AFTER_SECONDS = 10.0

# How many consecutive disconnected/auth-pending transitions one of the backend's own Upstox feed
# connections can have before it's worth a notification -- avoids notifying on a single transient
# reconnect, which is routine and self-healing.
_FEED_FAILURE_NOTIFY_THRESHOLD = 3

# How many consecutive _run_market_feed_staleness_check passes (each _MARKET_FEED_STALENESS_CHECK_
# INTERVAL_SECONDS apart) a single instrument can need re-subscribing before it's worth a
# notification -- see _MarketFeedStalenessNotifier's own doc comment for why a resend alone can't
# fix a persistent Upstox-side rejection.
_MARKET_FEED_STALENESS_NOTIFY_THRESHOLD = 3


@dataclass
class _MaxLossWatcherDeps:
    """Bundles what check_max_loss_now needs so _on_market_tick's on_tick lambda (called on
    every single live tick) doesn't need eight separate positional captures. Constructed once in
    the lifespan, passed by reference -- everything in it is either immutable for the app's
    lifetime or (exit_all_lock, notification_service) already safe to share across callers."""

    token_store: EncryptedTokenStore
    settings_store: MaxLossSettingsStore
    smart_order_service: SmartOrderService
    instrument_rules_service: InstrumentRulesService
    notification_service: NotificationService
    exit_all_lock: asyncio.Lock


@dataclass
class _OrderEngineMaxLossWatcherDeps:
    """[_MaxLossWatcherDeps]'s sibling for the new engine's own server-side watcher (§8.4
    milestone 6) -- kept separate rather than folded into that dataclass, same isolation-rule
    posture as the rest of Part 4: a different ledger, a different lock, a different order
    service, no shared state with the old engine's watcher at all."""

    token_store: EncryptedTokenStore
    ledger_store: OrderEngineLedgerStore
    lot_tracker: OrderEngineLotTracker
    order_service: OrderEngineOrderService
    notification_service: NotificationService
    exit_all_lock: asyncio.Lock


@dataclass
class _OrderEngineTriggerEvaluatorDeps:
    """§6.3's server-side bracket executor's own deps bundle -- same "avoid eight positional
    captures in _on_market_tick's lambda" reasoning as [_OrderEngineMaxLossWatcherDeps]. Reuses
    that dataclass's own token_store/ledger_store/lot_tracker/order_service/notification_service
    (all already constructed for the max-loss watcher) rather than duplicating them -- no lock
    here, unlike the max-loss watcher's `exit_all_lock`, since correctness comes from
    `OrderEngineLedgerStore.cas_update_trigger_rule_state`'s own database-level CAS, not from
    serializing callers (see `order_engine_trigger_evaluator`'s own header comment)."""

    token_store: EncryptedTokenStore
    ledger_store: OrderEngineLedgerStore
    lot_tracker: OrderEngineLotTracker
    order_service: OrderEngineOrderService
    notification_service: NotificationService


class _FeedStateNotifier:
    """Turns one `UpstoxWebSocketClient`'s `on_state_change` callbacks into a single notification
    once it's been unhealthy for `_FEED_FAILURE_NOTIFY_THRESHOLD` consecutive transitions in a
    row, then a single "recovered" notification the next time it reconnects -- never one
    notification per retry, since the client already retries every couple of seconds on its own.
    """

    def __init__(self, *, name: str, notification_service: NotificationService) -> None:
        self._name = name
        self._notification_service = notification_service
        self._consecutive_failures = 0
        self._notified = False

    def handle(self, state: str) -> None:
        if state == "connected":
            if self._notified:
                asyncio.create_task(
                    self._notification_service.record(
                        category="feed",
                        severity="info",
                        title=f"{self._name} reconnected",
                        message=f"{self._name} connection has been restored.",
                    )
                )
            self._consecutive_failures = 0
            self._notified = False
            return

        self._consecutive_failures += 1
        if self._consecutive_failures < _FEED_FAILURE_NOTIFY_THRESHOLD or self._notified:
            return
        self._notified = True
        if state == "auth_pending":
            message = f"{self._name} is waiting on a valid Upstox login to reconnect."
        else:
            message = f"{self._name} has failed to stay connected for {self._consecutive_failures} attempts in a row."
        asyncio.create_task(
            self._notification_service.record(
                category="feed",
                severity="warning",
                title=f"{self._name} disconnected",
                message=message,
            )
        )


class _MarketFeedStalenessNotifier:
    """Escalates a single instrument's market-feed staleness to a real notification once
    `resend_stale_subscriptions` has had to nudge it repeatedly, rather than only ever logging a
    warning on every `_run_market_feed_staleness_check` pass forever. A resend is a plain duplicate
    `sub` -- it fixes a transient drop, but if Upstox keeps rejecting the same key (exceeding the
    full-mode combined-category cap, generic rate limiting -- see
    `UpstoxMarketFeedClient._on_message`'s own doc comment), nudging it does nothing and today
    that was invisible outside the log file. Fires at most one notification per ongoing incident
    per key (not one per check), and a single "recovered" notification once a previously-notified
    key stops needing nudging.
    """

    def __init__(self, *, notification_service: NotificationService) -> None:
        self._notification_service = notification_service
        self._consecutive_nudges: dict[str, int] = {}
        self._notified: set[str] = set()

    def handle(self, nudged: list[str]) -> None:
        nudged_set = set(nudged)

        for key in nudged_set:
            count = self._consecutive_nudges.get(key, 0) + 1
            self._consecutive_nudges[key] = count
            if count >= _MARKET_FEED_STALENESS_NOTIFY_THRESHOLD and key not in self._notified:
                self._notified.add(key)
                asyncio.create_task(
                    self._notification_service.record(
                        category="feed",
                        severity="warning",
                        title="Market feed subscription stalled",
                        message=(
                            f"{key} has needed re-subscribing {count} times in a row and still "
                            "isn't receiving ticks -- likely a persistent Upstox-side rejection "
                            "(entitlement, the 50-instrument full-mode cap, or rate limiting)."
                        ),
                    )
                )

        # Anything that stopped needing a nudge this round: reset its streak, and notify recovery
        # only if it had actually escalated (a key that was nudged once or twice but never crossed
        # the threshold was never reported stalled in the first place, so no recovery to report).
        for key in list(self._consecutive_nudges):
            if key in nudged_set:
                continue
            self._consecutive_nudges.pop(key, None)
            was_notified = key in self._notified
            self._notified.discard(key)
            if was_notified:
                asyncio.create_task(
                    self._notification_service.record(
                        category="feed",
                        severity="info",
                        title="Market feed subscription recovered",
                        message=f"{key} is receiving ticks again.",
                    )
                )


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Shared, connection-pooled client for every outbound Upstox REST call -- UpstoxService falls
    # back to a fresh `httpx.AsyncClient` per request when none is given (see its own `_request`),
    # which means a brand new TLS handshake for every single call. That's the dominant cost behind
    # slow/hanging chart loads under the 3-pane layout: each pane's candles+futures-search chain is
    # several sequential Upstox calls, times 3 panes loading together, times a fresh handshake each
    # -- this one pooled client removes that multiplier for every route built on
    # `get_upstox_service` (see dependencies.py).
    upstox_http_client = httpx.AsyncClient(timeout=15.0)
    app.state.upstox_http_client = upstox_http_client
    # Constructed without a stream_manager up front -- one doesn't exist yet (it needs the feed
    # subscription manager, which needs the market feed client, which itself wants this same
    # notification_service for its own state-change notifications). Patched onto this instance
    # once stream_manager is built below; every background task started here holds this same
    # object by reference, so the patch is visible to all of them without re-wiring anything.
    notification_service = NotificationService(settings)
    # Cheap to construct even with no Firebase service account configured -- see FcmService's own
    # doc comment for why it stays a permanent no-op in that case rather than needing a feature
    # flag here.
    fcm_service = FcmService(settings)
    notification_service.fcm_service = fcm_service

    # Shared by POST /orders/exit-all|exit-positions and max_loss_watcher below -- see
    # get_exit_all_lock's own doc comment for why a client-triggered flatten and the watcher's own
    # must never run concurrently against the same open positions.
    exit_all_lock = asyncio.Lock()
    app.state.exit_all_lock = exit_all_lock

    # See TrackedInstrumentsStore / run_tracked_instruments_poller's own doc comment for why this
    # exists -- keeps 5-minute-change history warm for Settings-picked underlyings even while no
    # client is actively polling. Cancelled cleanly on shutdown, same as any other background task
    # tied to the app's own lifetime.
    poller_task = asyncio.create_task(run_tracked_instruments_poller(settings))
    oi_collector_task = asyncio.create_task(run_oi_snapshot_collector(settings))
    atm_iv_collector_task = asyncio.create_task(run_atm_iv_snapshot_collector(settings))
    account_snapshot_task = asyncio.create_task(
        run_account_snapshot_scheduler(settings, notification_service),
    )
    auth_watchdog_task = asyncio.create_task(run_auth_watchdog(settings, notification_service))
    notification_retention_task = asyncio.create_task(run_notification_retention(settings))
    # See gtt_status_poller.py's own doc comment -- background-only reconciliation now that
    # place/modify/cancel (smart_order_service.py) write directly to GttHistoryStore themselves.
    gtt_status_poller_task = asyncio.create_task(run_gtt_status_poller(settings))

    # Backend-side backstop for max-loss auto square-off -- reacts even if the app is closed,
    # backgrounded, or offline, and (see check_max_loss_now's own doc comment) reacts to live
    # market ticks rather than a REST-polling timer. Dedicated token store/UpstoxService instances,
    # same posture as market_feed_token_store/portfolio_feed_token_store below.
    max_loss_token_store = EncryptedTokenStore(settings)
    max_loss_upstox = UpstoxService(settings, client=upstox_http_client)
    position_tracker = PositionPnlTracker(max_loss_upstox, max_loss_token_store)
    max_loss_settings_store = MaxLossSettingsStore(settings)
    # No history_store here (unlike the route-level SmartOrderService constructions in routes.py)
    # -- keeps _cancel_stray_gtts's own lookup on its established live-Upstox-read fallback path
    # (self.history_store is None), rather than pulling this watcher's own stray-GTT cleanup into
    # this pass's scope. GttHistoryStore.record_placed/record_modified/record_cancelled -- the
    # actual fix -- still fire for every order this backend places/modifies/cancels regardless of
    # which SmartOrderService instance runs them, since those routes always pass a store.
    max_loss_smart_order_service = SmartOrderService(max_loss_upstox)
    max_loss_instrument_rules_service = InstrumentRulesService(settings)

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
        # FIX: this callback fires synchronously, inline, from inside the market feed's own
        # WebSocket read loop (UpstoxWebSocketClient._connect_once -> _on_message -> ... ->
        # LiveCandleBuilder.handle_tick) -- calling _persist_completed_candle directly here used
        # to run CandleCacheStore.save's blocking sqlite3.connect/executemany right there. Python's
        # single-threaded event loop means a slow or stuck disk write didn't just delay this one
        # candle -- it froze EVERY coroutine in the process, including the read loop's own
        # stale-frame watchdog (nothing could even notice the freeze to reconnect), matching a
        # real production report of all live prices freezing with only a full container restart
        # recovering it. asyncio.to_thread moves the actual blocking I/O off the event loop
        # entirely; create_task (same fire-and-forget posture _on_market_tick's own
        # dispatch_tick/check_max_loss_now calls already use below) schedules it without blocking
        # the tick that triggered it.
        on_candle_completed=lambda instrument_key, candle: asyncio.create_task(
            asyncio.to_thread(_persist_completed_candle, candle_cache_store, instrument_key, candle),
        ),
    )
    order_fill_detector = OrderFillDetector()
    order_flow_service = OrderFlowService()
    journal_store = JournalStore(settings)
    # §8's server-side live-PnL tracker (docs/ORDER_POSITION_OVERHAUL_DESIGN.md §8) -- reads the
    # same ledger the order-engine routes write to, cheap enough to query per-tick since it's a
    # local SQLite file, not a network call (see OrderEngineLotTracker's own doc comment).
    order_engine_ledger_store = OrderEngineLedgerStore(settings)
    order_engine_lot_tracker = OrderEngineLotTracker(order_engine_ledger_store)
    # §8.4 milestones 5/6's live callers -- dedicated token store/UpstoxService instances, same
    # posture as max_loss_token_store/max_loss_upstox above, and a dedicated exit_all_lock since
    # this flattens against an entirely different ledger than the old engine's own
    # exit_all_lock/position_tracker guard.
    order_engine_token_store = EncryptedTokenStore(settings)
    order_engine_upstox = UpstoxService(settings, client=upstox_http_client)
    order_engine_order_service = OrderEngineOrderService(order_engine_upstox)
    order_engine_exit_all_lock = asyncio.Lock()
    exit_reconciliation_checker = ExitReconciliationChecker(order_engine_ledger_store, order_engine_upstox)
    # Part 5 (docs/ORDER_HISTORY_V2_DESIGN.md) -- the server-derived `order_history`/`lots`
    # writer, replacing the client's `PUT /order-engine/ledger/lots` mirror as the sole authority.
    order_history_recorder = OrderHistoryRecorder(order_engine_ledger_store)
    trade_context_token_store = EncryptedTokenStore(settings)
    trade_context_upstox = UpstoxService(settings, client=upstox_http_client)
    trade_context_service = TradeContextService(
        store=journal_store,
        upstox=trade_context_upstox,
        signals=UnderlyingSignalsService(
            trade_context_upstox,
            snapshot_store=SignalSnapshotStore(settings),
            oi_snapshot_store=OISnapshotStore(settings),
        ),
    )
    journal_reconciler = JournalReconciler(
        store=journal_store,
        upstox=trade_context_upstox,
        token_store=trade_context_token_store,
        notifications=notification_service,
    )
    market_feed_notifier = _FeedStateNotifier(
        name="Market data feed", notification_service=notification_service,
    )
    portfolio_feed_notifier = _FeedStateNotifier(
        name="Portfolio feed", notification_service=notification_service,
    )

    market_feed_token_store = EncryptedTokenStore(settings)
    market_feed_upstox = UpstoxService(settings, client=upstox_http_client)
    market_feed_client = UpstoxMarketFeedClient(
        upstox=market_feed_upstox,
        token_store=market_feed_token_store,
        on_tick=lambda tick: _on_market_tick(
            candle_builder, stream_manager, position_tracker, max_loss_watcher_deps,
            order_flow_service, order_engine_lot_tracker, order_engine_max_loss_watcher_deps,
            order_engine_trigger_evaluator_deps, tick,
        ),
        on_state_change=market_feed_notifier.handle,
    )
    portfolio_feed_token_store = EncryptedTokenStore(settings)
    portfolio_feed_upstox = UpstoxService(settings, client=upstox_http_client)
    portfolio_feed_client = UpstoxPortfolioFeedClient(
        upstox=portfolio_feed_upstox,
        token_store=portfolio_feed_token_store,
        on_order_update=lambda payload: _on_portfolio_update(
            order_fill_detector,
            stream_manager,
            notification_service,
            position_tracker,
            subscription_manager,
            trade_context_service,
            trade_context_token_store,
            journal_reconciler,
            order_engine_ledger_store,
            exit_reconciliation_checker,
            order_history_recorder,
            order_engine_upstox,
            order_engine_token_store,
            payload,
        ),
        on_state_change=portfolio_feed_notifier.handle,
    )

    tracked_store = TrackedInstrumentsStore(settings)
    subscription_manager = FeedSubscriptionManager(
        market_feed_client=market_feed_client, tracked_store=tracked_store,
    )
    stream_manager = StreamConnectionManager(
        settings=settings,
        subscription_manager=subscription_manager,
        notification_service=notification_service,
    )
    notification_service.stream_manager = stream_manager

    # Bundled together purely so _on_market_tick's lambda above doesn't need eight positional
    # captures -- unpacked at each call site instead.
    max_loss_watcher_deps = _MaxLossWatcherDeps(
        token_store=max_loss_token_store,
        settings_store=max_loss_settings_store,
        smart_order_service=max_loss_smart_order_service,
        instrument_rules_service=max_loss_instrument_rules_service,
        notification_service=notification_service,
        exit_all_lock=exit_all_lock,
    )
    order_engine_max_loss_watcher_deps = _OrderEngineMaxLossWatcherDeps(
        token_store=order_engine_token_store,
        ledger_store=order_engine_ledger_store,
        lot_tracker=order_engine_lot_tracker,
        order_service=order_engine_order_service,
        notification_service=notification_service,
        exit_all_lock=order_engine_exit_all_lock,
    )
    order_engine_trigger_evaluator_deps = _OrderEngineTriggerEvaluatorDeps(
        token_store=order_engine_token_store,
        ledger_store=order_engine_ledger_store,
        lot_tracker=order_engine_lot_tracker,
        order_service=order_engine_order_service,
        notification_service=notification_service,
    )

    app.state.order_engine_lot_tracker = order_engine_lot_tracker
    app.state.market_feed_client = market_feed_client
    app.state.portfolio_feed_client = portfolio_feed_client
    app.state.feed_subscription_manager = subscription_manager
    app.state.live_candle_builder = candle_builder
    app.state.stream_manager = stream_manager
    app.state.notification_service = notification_service
    app.state.fcm_service = fcm_service
    app.state.journal_reconciler = journal_reconciler
    journal_reconciler_task = asyncio.create_task(run_journal_reconciler(journal_reconciler))
    asyncio.create_task(journal_reconciler.reconcile())


    app.state.journal_reconciler = journal_reconciler                                                         
    journal_reconciler_task = asyncio.create_task(run_journal_reconciler(journal_reconciler))                 
    asyncio.create_task(journal_reconciler.reconcile())                                                       
    auto_login_task = asyncio.create_task(                                                                    
        run_auto_login_scheduler(settings, notification_service, journal_reconciler),                         
    ) 


    # Best-effort initial fill so open positions are already subscribed (and their live P&L
    # already known) before the very first tick, not just from whenever the periodic refresh
    # below happens to next run.
    with contextlib.suppress(Exception):
        await position_tracker.refresh()
        await subscription_manager.set_open_position_instruments(position_tracker.instrument_keys())
        await subscription_manager.set_order_engine_instruments(order_engine_lot_tracker.instrument_keys())

    market_feed_client.start()
    portfolio_feed_client.start()
    subscription_refresh_task = asyncio.create_task(
        _run_subscription_refresh(subscription_manager),
    )
    position_tracker_refresh_task = asyncio.create_task(
        _run_position_tracker_refresh(position_tracker, subscription_manager),
    )
    order_engine_subscription_refresh_task = asyncio.create_task(
        _run_order_engine_subscription_refresh(order_engine_lot_tracker, subscription_manager),
    )
    market_feed_staleness_notifier = _MarketFeedStalenessNotifier(
        notification_service=notification_service,
    )
    market_feed_staleness_task = asyncio.create_task(
        _run_market_feed_staleness_check(market_feed_client, market_feed_staleness_notifier),
    )
    max_loss_watcher_task = asyncio.create_task(
        run_max_loss_watcher_fallback(
            token_store=max_loss_watcher_deps.token_store,
            settings_store=max_loss_watcher_deps.settings_store,
            tracker=position_tracker,
            smart_order_service=max_loss_watcher_deps.smart_order_service,
            instrument_rules_service=max_loss_watcher_deps.instrument_rules_service,
            notification_service=max_loss_watcher_deps.notification_service,
            exit_all_lock=max_loss_watcher_deps.exit_all_lock,
        ),
    )
    # §8.4 milestone 6's backstop -- check_now itself now also runs on every live tick that
    # touches an open order-engine lot, see _on_market_tick; this loop only matters for stretches
    # with no ticks, same "backstop, not primary" relationship the old engine's own
    # max_loss_watcher_task has to check_max_loss_now.
    order_engine_max_loss_watcher_task = asyncio.create_task(
        run_order_engine_max_loss_watcher_fallback(
            token_store=order_engine_max_loss_watcher_deps.token_store,
            ledger_store=order_engine_max_loss_watcher_deps.ledger_store,
            lot_tracker=order_engine_max_loss_watcher_deps.lot_tracker,
            order_engine_order_service=order_engine_max_loss_watcher_deps.order_service,
            notification_service=order_engine_max_loss_watcher_deps.notification_service,
            exit_all_lock=order_engine_max_loss_watcher_deps.exit_all_lock,
        ),
    )
    # §6.3's backstop -- check_now itself now also runs on every live tick that touches an open
    # order-engine lot, see _on_market_tick; this loop only matters for stretches with no ticks,
    # same "backstop, not primary" relationship the max-loss watcher's own fallback loop has.
    order_engine_trigger_evaluator_task = asyncio.create_task(
        run_order_engine_trigger_evaluator_fallback(
            token_store=order_engine_trigger_evaluator_deps.token_store,
            ledger_store=order_engine_trigger_evaluator_deps.ledger_store,
            lot_tracker=order_engine_trigger_evaluator_deps.lot_tracker,
            order_engine_order_service=order_engine_trigger_evaluator_deps.order_service,
            notification_service=order_engine_trigger_evaluator_deps.notification_service,
        ),
    )

    await notification_service.record(
        category="system",
        severity="info",
        title="Backend restarted",
        message="The trading backend process has started.",
    )

    try:
        yield
    finally:
        poller_task.cancel()
        oi_collector_task.cancel()
        atm_iv_collector_task.cancel()
        account_snapshot_task.cancel()
        auth_watchdog_task.cancel()
        notification_retention_task.cancel()
        gtt_status_poller_task.cancel()
        max_loss_watcher_task.cancel()
        order_engine_max_loss_watcher_task.cancel()
        order_engine_trigger_evaluator_task.cancel()
        subscription_refresh_task.cancel()
        position_tracker_refresh_task.cancel()
        order_engine_subscription_refresh_task.cancel()
        market_feed_staleness_task.cancel()
        journal_reconciler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller_task
        with contextlib.suppress(asyncio.CancelledError):
            await oi_collector_task
        with contextlib.suppress(asyncio.CancelledError):
            await atm_iv_collector_task
        with contextlib.suppress(asyncio.CancelledError):
            await account_snapshot_task
        with contextlib.suppress(asyncio.CancelledError):
            await auth_watchdog_task
        with contextlib.suppress(asyncio.CancelledError):
            await notification_retention_task
        with contextlib.suppress(asyncio.CancelledError):
            await gtt_status_poller_task
        with contextlib.suppress(asyncio.CancelledError):
            await max_loss_watcher_task
        with contextlib.suppress(asyncio.CancelledError):
            await order_engine_max_loss_watcher_task
        with contextlib.suppress(asyncio.CancelledError):
            await order_engine_trigger_evaluator_task
        with contextlib.suppress(asyncio.CancelledError):
            await subscription_refresh_task
        with contextlib.suppress(asyncio.CancelledError):
            await position_tracker_refresh_task
        with contextlib.suppress(asyncio.CancelledError):
            await order_engine_subscription_refresh_task
        with contextlib.suppress(asyncio.CancelledError):
            await market_feed_staleness_task
        with contextlib.suppress(asyncio.CancelledError):
            await journal_reconciler_task
        await market_feed_client.stop()
        await portfolio_feed_client.stop()
        await upstox_http_client.aclose()


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


async def _run_position_tracker_refresh(
    tracker: PositionPnlTracker,
    subscription_manager: FeedSubscriptionManager,
) -> None:
    """Periodic structural refresh for PositionPnlTracker -- a fallback only, since
    _on_portfolio_update already triggers an immediate refresh the moment any order actually
    changes. Catches whatever that path might miss (e.g. a position opened by some means other
    than a normal order-placement flow) within this interval instead of indefinitely."""
    while True:
        await asyncio.sleep(_POSITION_TRACKER_REFRESH_INTERVAL_SECONDS)
        try:
            await tracker.refresh()
            await subscription_manager.set_open_position_instruments(tracker.instrument_keys())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Position P&L tracker refresh failed unexpectedly", exc_info=True)


async def _run_order_engine_subscription_refresh(
    lot_tracker: OrderEngineLotTracker,
    subscription_manager: FeedSubscriptionManager,
) -> None:
    """[_run_position_tracker_refresh]'s sibling for the order engine's own open-lot set (see
    `FeedSubscriptionManager`'s own doc comment, source 4) -- a fallback only, since
    `_on_portfolio_update` already triggers an immediate push the moment any order-engine order
    actually changes (a fresh entry fill, an exit closing a lot). Unlike the old engine's own
    refresh, there's no network re-fetch here -- `OrderEngineLotTracker.instrument_keys()` reads
    the local ledger directly, always current -- this loop exists purely to catch a lot opened by
    some path that didn't happen to trigger an immediate push, same "backstop, not primary"
    posture as everything else server-side already follows for this engine."""
    while True:
        await asyncio.sleep(_POSITION_TRACKER_REFRESH_INTERVAL_SECONDS)
        try:
            await subscription_manager.set_order_engine_instruments(lot_tracker.instrument_keys())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Order-engine subscription refresh failed unexpectedly", exc_info=True)


async def _run_market_feed_staleness_check(
    market_feed_client: UpstoxMarketFeedClient,
    staleness_notifier: _MarketFeedStalenessNotifier,
) -> None:
    """Best-effort self-heal for a silently-dropped single-instrument subscription -- see
    UpstoxMarketFeedClient.resend_stale_subscriptions' own doc comment for why Upstox's sub/unsub
    acks/rejections are otherwise invisible to this backend, and why the connection-wide
    stale-frame watchdog in UpstoxWebSocketClient can't catch this on its own. Market-hours gated
    since there's nothing subscribed worth nudging outside trading hours. staleness_notifier gets
    the nudged list on every pass (even when empty) so it can also detect recovery -- see its own
    doc comment for why a resend alone can't fix a persistent rejection."""
    while True:
        await asyncio.sleep(_MARKET_FEED_STALENESS_CHECK_INTERVAL_SECONDS)
        try:
            if not is_market_open():
                continue
            d30_action = await market_feed_client.recover_stale_d30_cohort(
                stale_after_seconds=_D30_COHORT_STALE_AFTER_SECONDS,
                reconnect_after_seconds=_D30_COHORT_RECONNECT_AFTER_SECONDS,
            )
            if d30_action == "d30_unsub_resub":
                logger.warning("Market feed self-heal: resetting the complete Full D30 cohort")
            elif d30_action == "upstream_reconnect":
                logger.warning(
                    "Market feed self-heal: Full D30 stayed stale; replaced upstream Upstox socket",
                )
            nudged = await market_feed_client.resend_stale_subscriptions(
                _MARKET_FEED_INSTRUMENT_STALE_AFTER_SECONDS,
                include_d30=d30_action is None,
            )
            if nudged:
                logger.warning("Market feed self-heal: resubscribed stale instrument(s) %s", nudged)
            staleness_notifier.handle(nudged)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Market feed staleness check failed unexpectedly", exc_info=True)


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
    position_tracker: PositionPnlTracker,
    max_loss_watcher_deps: _MaxLossWatcherDeps,
    order_flow_service: OrderFlowService,
    order_engine_lot_tracker: OrderEngineLotTracker,
    order_engine_max_loss_watcher_deps: _OrderEngineMaxLossWatcherDeps,
    order_engine_trigger_evaluator_deps: _OrderEngineTriggerEvaluatorDeps,
    tick: FeedTick,
) -> None:
    live_candle = candle_builder.handle_tick(tick)
    if live_candle is not None:
        tick = replace(tick, one_minute_candle=live_candle)
    asyncio.create_task(stream_manager.dispatch_tick(tick))

    order_flow_snapshot = order_flow_service.handle_tick(tick)
    if order_flow_snapshot is not None:
        asyncio.create_task(
            stream_manager.dispatch_order_flow(tick.instrument_key, order_flow_snapshot.to_dict()),
        )

    # §8's live-PnL push -- a no-op call for the (overwhelming majority of) ticks on an instrument
    # with no open new-engine lot at all, since dispatch_order_engine_lot_status itself no-ops on
    # an empty list.
    lot_statuses = order_engine_lot_tracker.apply_tick(tick.instrument_key, tick.ltp)
    if lot_statuses:
        asyncio.create_task(stream_manager.dispatch_order_engine_lot_status(lot_statuses))

    # §8.4 milestone 6, wired to a live caller: only worth checking on a tick that actually
    # touches an open order-engine lot, same "gate on the relevant instrument set" posture the old
    # engine's own check_max_loss_now call below already uses against position_tracker.
    if tick.instrument_key in order_engine_lot_tracker.instrument_keys():
        asyncio.create_task(
            check_order_engine_max_loss_now(
                now=datetime.now(timezone.utc),
                token_store=order_engine_max_loss_watcher_deps.token_store,
                ledger_store=order_engine_max_loss_watcher_deps.ledger_store,
                lot_tracker=order_engine_max_loss_watcher_deps.lot_tracker,
                order_engine_order_service=order_engine_max_loss_watcher_deps.order_service,
                notification_service=order_engine_max_loss_watcher_deps.notification_service,
                exit_all_lock=order_engine_max_loss_watcher_deps.exit_all_lock,
            ),
        )
        # §6.3's server-side bracket executor, wired to a live caller: same "gate on the relevant
        # instrument set" posture as the max-loss check just above -- every open order-engine lot's
        # instrument is exactly the set an armed trigger_rule could belong to (a rule always
        # belongs to a lot, see OrderEngineLedgerStore's own schema).
        asyncio.create_task(
            check_order_engine_triggers_now(
                tick.instrument_key,
                tick.ltp,
                now=datetime.now(timezone.utc),
                token_store=order_engine_trigger_evaluator_deps.token_store,
                ledger_store=order_engine_trigger_evaluator_deps.ledger_store,
                order_engine_order_service=order_engine_trigger_evaluator_deps.order_service,
                notification_service=order_engine_trigger_evaluator_deps.notification_service,
            ),
        )

    position_tracker.apply_tick(tick.instrument_key, tick.ltp)
    if tick.instrument_key in position_tracker.instrument_keys():
        asyncio.create_task(
            check_max_loss_now(
                now=datetime.now(timezone.utc),
                token_store=max_loss_watcher_deps.token_store,
                settings_store=max_loss_watcher_deps.settings_store,
                tracker=position_tracker,
                smart_order_service=max_loss_watcher_deps.smart_order_service,
                instrument_rules_service=max_loss_watcher_deps.instrument_rules_service,
                notification_service=max_loss_watcher_deps.notification_service,
                exit_all_lock=max_loss_watcher_deps.exit_all_lock,
            ),
        )


def _on_portfolio_update(
    detector: OrderFillDetector,
    stream_manager: StreamConnectionManager,
    notification_service: NotificationService,
    position_tracker: PositionPnlTracker,
    subscription_manager: FeedSubscriptionManager,
    trade_context_service: TradeContextService,
    trade_context_token_store: EncryptedTokenStore,
    journal_reconciler: JournalReconciler,
    order_engine_ledger_store: OrderEngineLedgerStore,
    exit_reconciliation_checker: ExitReconciliationChecker,
    order_history_recorder: OrderHistoryRecorder,
    order_engine_upstox: UpstoxService,
    order_engine_token_store: EncryptedTokenStore,
    payload: dict[str, Any],
) -> None:
    async def _refresh_position_tracker() -> None:
        try:
            await position_tracker.refresh()
            await subscription_manager.set_open_position_instruments(
                position_tracker.instrument_keys(),
            )
        except Exception:
            logger.warning("Position P&L tracker refresh (on order update) failed", exc_info=True)

    async def _refresh_order_engine_subscription() -> None:
        try:
            # No network re-fetch needed -- get_open_lots() reads the local ledger directly,
            # already current the moment order_history_recorder.apply_fill_to_ledger (called
            # further below in this same handler) returns.
            instrument_keys = {lot["instrument_key"] for lot in order_engine_ledger_store.get_open_lots()}
            await subscription_manager.set_order_engine_instruments(instrument_keys)
        except Exception:
            logger.warning("Order-engine subscription refresh (on order update) failed", exc_info=True)

    # Any order-related event (new fill, rejection, modification) can mean the open-position set
    # itself just changed -- refresh immediately rather than waiting for the next periodic tick,
    # so a newly opened position starts getting live max-loss coverage right away.
    asyncio.create_task(_refresh_position_tracker())
    # Same immediacy for a newly opened order-engine lot -- see _run_order_engine_subscription_refresh's
    # own doc comment for why this one needs no network re-fetch, just a fresh local read.
    asyncio.create_task(_refresh_order_engine_subscription())
    asyncio.create_task(journal_reconciler.reconcile())

    is_new_fill = _handle_order_update(detector, payload)
    if is_new_fill:
        symbol = payload.get("trading_symbol") or payload.get("instrument_token") or "position"
        asyncio.create_task(
            notification_service.record(
                category="orders",
                severity="info",
                title="Order filled",
                message=f"Order for {symbol} completed.",
                details={"order_id": payload.get("order_id"), "status": payload.get("status")},
            )
        )
        order_id = payload.get("order_id")
        instrument_key = payload.get("instrument_token")
        if isinstance(order_id, str) and order_id and trade_context_token_store.has_token():
            async def _capture_fill_context() -> None:
                try:
                    await trade_context_service.capture_fill_from_placement(
                        access_token=trade_context_token_store.load_access_token(),
                        order_id=order_id,
                        instrument_key=instrument_key if isinstance(instrument_key, str) else None,
                        contract_ltp=_payload_price(payload),
                    )
                except Exception:
                    logger.warning("Fill trade-context capture failed", exc_info=True)

            asyncio.create_task(_capture_fill_context())
    elif payload.get("status") == "rejected":
        symbol = payload.get("trading_symbol") or payload.get("instrument_token") or "position"
        reason = payload.get("status_message") or "No reason given by Upstox."
        asyncio.create_task(
            notification_service.record(
                category="orders",
                severity="warning",
                title="Order rejected",
                message=f"Order for {symbol} was rejected: {reason}",
                details={"order_id": payload.get("order_id"), "status_message": reason},
            )
        )
    # Every order update (not just a newly-detected fill) is forwarded -- the app needs to see
    # a bracket move from "open" to "cancelled"/"rejected" too, not only "complete" transitions;
    # OrderFillDetector's own edge-detection is specifically about when to play the fill sound,
    # a client-side concern, not a filter on what state changes reach the app at all.
    asyncio.create_task(stream_manager.dispatch_order_update(payload))

    # §8.4 milestone 5, wired to a live caller: a "complete" update whose tag matches one of the
    # new engine's own currently-PLACED bracket legs is an exit fill worth reconciling against
    # broker truth. Fire-and-forget, same posture as every other side-effect in this handler --
    # check_lot itself records the MATCHED/MISMATCH event, nothing here needs the result.
    if is_new_fill:
        asyncio.create_task(
            _reconcile_order_engine_exit(
                order_engine_ledger_store, exit_reconciliation_checker, order_engine_token_store, payload,
            ),
        )

    # Part 5 (docs/ORDER_HISTORY_V2_DESIGN.md) -- records *every* order sighting (not just new
    # fills) into `order_history`, and derives `lots` server-side on a genuine fill. Independent
    # of and in addition to the exit-only reconciliation above.
    asyncio.create_task(
        _record_order_history_from_push(
            order_engine_ledger_store, order_history_recorder, order_engine_upstox,
            order_engine_token_store, payload,
        ),
    )


async def _record_order_history_from_push(
    ledger_store: OrderEngineLedgerStore,
    recorder: OrderHistoryRecorder,
    upstox: UpstoxService,
    token_store: EncryptedTokenStore,
    payload: dict[str, Any],
) -> None:
    """Part 5's own reactive writer: never trusts [payload] (the raw portfolio-feed WS push)
    directly for a durable write -- re-fetches the order from Upstox's order book first, same
    "broker ground truth, always re-confirmed, never taken on faith" discipline
    `ExitReconciliationChecker`/`_reconcile_order_engine_exit` already use. Records an
    `order_history` row for every status (open/complete/rejected/cancelled), and on a `complete`
    status with a resolved role, derives `lots` too.

    Role resolution: exits are recovered by scanning currently-`PLACED` trigger rules (an exit
    order's tag is derived from the firing `TriggerRule.id`, so a matching rule directly gives
    both role and owning lot). Entries can't be recovered the same way -- their tag is derived
    from a client-generated idempotency key this server never independently learns, since
    reversing a hash isn't possible. Instead, `place_order_engine_order`
    (`app/api/order_engine_routes.py`) writes a placement-time correlation row via
    `OrderHistoryRecorder.record_placement` whenever its caller supplies a `role` -- this falls
    back to reading that row by `broker_order_id` when the trigger-rule scan finds nothing. An
    entry order placed without that `role` hint still gets recorded in `order_history` (role
    `None`) but never auto-creates a lot, same as before this fallback existed."""
    order_id = payload.get("order_id") or payload.get("exchange_order_id")
    if not isinstance(order_id, str) or not order_id:
        return
    if not token_store.has_token():
        return
    try:
        access_token = token_store.load_access_token()
    except (TokenStoreError, UpstoxAuthRequiredError):
        return

    try:
        broker_order = await find_order_by_id(upstox, access_token, order_id)
        if broker_order is None:
            return

        idempotency_key: Optional[str] = None
        lot_id: Optional[str] = None
        role: Optional[str] = None
        tag = broker_order.get("tag")
        if isinstance(tag, str) and tag:
            for rule in ledger_store.get_trigger_rules_by_state("PLACED"):
                rule_id = rule.get("id")
                if isinstance(rule_id, str) and derive_order_tag(rule_id) == tag:
                    idempotency_key = rule_id
                    lot_id = rule.get("lot_id")
                    role = "EXIT"
                    break

        placed_row: Optional[dict[str, Any]] = None
        if role is None:
            # No PLACED bracket leg matched -- fall back to whatever place_order_engine_order
            # recorded at placement time (entries, and any other caller that supplied a role).
            placed_row = ledger_store.get_order_by_broker_order_id(order_id)
            if placed_row is not None:
                idempotency_key = placed_row.get("idempotency_key") or idempotency_key
                role = placed_row.get("role") or role
                if role == "ENTRY":
                    # record_placement reuses idempotency_key as the eventual lot id -- stable,
                    # unique per placement, no separate id-generation scheme needed.
                    lot_id = placed_row.get("lot_id") or idempotency_key

        # Fill-application runs *before* the order_history write below: for an ENTRY, the lot
        # doesn't exist yet until apply_fill_to_ledger creates it, and order_history.lot_id is a
        # real FK (same DB file, Part 5's own choice) -- referencing a lot_id that doesn't exist
        # yet would fail that constraint. An EXIT's lot already exists (created by its own prior
        # entry), so it's always safe to reference immediately.
        lot_confirmed_to_exist = None
        if broker_order.get("status") == "complete" and role in ("ENTRY", "EXIT") and lot_id:
            lot = ledger_store.get_lot(lot_id)
            entry_transaction_type = lot.get("transaction_type") if lot else None
            # §6.3 Part B2: the bracket this ENTRY placement itself intended, carried on
            # placed_row since record_placement's own call time -- see that method's own doc
            # comment. None for an EXIT (placed_row is never fetched on that path -- role came
            # from the PLACED-rule scan above instead) or an ENTRY placed with no bracket at all.
            lot_confirmed_to_exist = recorder.apply_fill_to_ledger(
                broker_order, lot_id=lot_id, role=role,
                entry_transaction_type=entry_transaction_type,
                target_price=placed_row.get("target_price") if placed_row else None,
                stoploss_price=placed_row.get("stoploss_price") if placed_row else None,
                trailing_gap=placed_row.get("trailing_gap") if placed_row else None,
            )

        snapshot_lot_id = lot_id if (role == "EXIT" or lot_confirmed_to_exist is not None) else None
        recorder.record_order_snapshot(
            broker_order, idempotency_key=idempotency_key, lot_id=snapshot_lot_id, role=role,
        )
    except Exception:
        logger.warning("Order-history recording failed for order %s", order_id, exc_info=True)


async def _reconcile_order_engine_exit(
    ledger_store: OrderEngineLedgerStore,
    checker: ExitReconciliationChecker,
    token_store: EncryptedTokenStore,
    payload: dict[str, Any],
) -> None:
    """§8.4 milestone 5's live-wiring: matches a newly-`complete` portfolio-feed order update
    against one of the new engine's own `PLACED` bracket legs and, on a match, reconciles that
    lot's realized P&L against broker truth (see `ExitReconciliationChecker.check_lot`'s own doc
    comment for the actual formula/mismatch classification).

    The match itself has to be a scan, not a lookup: Upstox's `tag` field on the payload is
    [derive_order_tag]'s one-way hash-truncation of a `TriggerRule.id`, not the id itself, so the
    only way back from a tag to a rule is recomputing the derived tag for every currently-`PLACED`
    rule and comparing (`ExitFillListener`, client-side, does the analogous work against its own
    much smaller local Room table -- this is the server-side equivalent, scoped to `PLACED` rows
    for the same reason). An update with no tag, or a tag that matches nothing (a manual
    broker-side order, or the old GTT-based path), is silently ignored -- same posture
    `ExitFillListener`/`EntryFillListener` already use for an unrelated update."""
    tag = payload.get("tag")
    order_id = payload.get("order_id") or payload.get("exchange_order_id")
    if not isinstance(tag, str) or not tag or not isinstance(order_id, str) or not order_id:
        return

    lot_id: Optional[str] = None
    for rule in ledger_store.get_trigger_rules_by_state("PLACED"):
        rule_id = rule.get("id")
        if isinstance(rule_id, str) and derive_order_tag(rule_id) == tag:
            lot_id = rule.get("lot_id")
            break
    if not lot_id:
        return

    if not token_store.has_token():
        return
    try:
        access_token = token_store.load_access_token()
    except (TokenStoreError, UpstoxAuthRequiredError):
        return

    try:
        await checker.check_lot(access_token, lot_id, order_id)
    except Exception:
        logger.warning(
            "Order-engine exit reconciliation failed for lot %s / order %s", lot_id, order_id,
            exc_info=True,
        )


def _payload_price(payload: dict[str, Any]) -> Optional[float]:
    for key in ("average_price", "price"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


app = FastAPI(title="Upstox Scalper Backend", version="0.1.0", lifespan=_lifespan)

# Android's OkHttp calls are never subject to same-origin/CORS at all, so this is purely additive
# for the web client -- an empty web_client_origin (the default) means CORSMiddleware is simply
# never added, leaving today's behavior completely unchanged.
_cors_origin = get_settings().web_client_origin
if _cors_origin:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_cors_origin],
        allow_credentials=True,
        # GET/POST covered reads and order placement, but the web client also modifies/cancels
        # GTT brackets (PUT /orders/gtt/modify, PUT /orders/modify, DELETE /orders/gtt/cancel) --
        # those methods were never added here, so the browser's own CORS preflight for them was
        # rejected before the actual request was ever sent, surfacing client-side as a generic
        # "Failed to fetch" with no server-side trace at all (dev tools' Network tab shows the
        # blocked OPTIONS preflight, not a real request/response).
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-API-Key"],
    )

app.include_router(api_router, prefix="/api")
app.include_router(stream_router, prefix="/api")
app.include_router(order_engine_router, prefix="/api")
app.include_router(journal_v2_router, prefix="/api")


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

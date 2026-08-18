from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from app.core.config import Settings, get_settings
from app.core.exceptions import TokenStoreError
from app.services.candle_cache_store import CandleCacheStore
from app.services.device_token_store import DeviceTokenStore
from app.services.max_loss_settings_store import MaxLossSettingsStore
from app.services.journal_store import JournalStore
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker
from app.services.notification_service import NotificationService
from app.services.notification_store import NotificationStore
from app.services.atm_iv_snapshot_store import AtmIvSnapshotStore
from app.services.oi_snapshot_store import OISnapshotStore
from app.services.signal_snapshot_store import SignalSnapshotStore
from app.services.token_store import EncryptedTokenStore
from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.upstox_service import UpstoxService
from app.services.usd_inr_service import UsdInrService
from app.services.watchlist_store import WatchlistStore


def get_token_store(settings: Settings = Depends(get_settings)) -> EncryptedTokenStore:
    """Create the encrypted token store for the current request."""
    try:
        return EncryptedTokenStore(settings)
    except TokenStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": str(exc)},
        ) from exc


def get_tracked_instruments_store(settings: Settings = Depends(get_settings)) -> TrackedInstrumentsStore:
    """Create the tracked-instruments store for the current request."""
    return TrackedInstrumentsStore(settings)


def get_watchlist_store(settings: Settings = Depends(get_settings)) -> WatchlistStore:
    """Create the watchlist store for the current request."""
    return WatchlistStore(settings)


def get_signal_snapshot_store(settings: Settings = Depends(get_settings)) -> SignalSnapshotStore:
    """Create the SQLite-backed underlying-signal history store for a request."""
    return SignalSnapshotStore(settings)


def get_oi_snapshot_store(settings: Settings = Depends(get_settings)) -> OISnapshotStore:
    """Create the SQLite-backed per-strike OI snapshot store for a request."""
    return OISnapshotStore(settings)


def get_atm_iv_snapshot_store(settings: Settings = Depends(get_settings)) -> AtmIvSnapshotStore:
    """Create the SQLite-backed ATM IV snapshot store for a request."""
    return AtmIvSnapshotStore(settings)


def get_candle_cache_store(settings: Settings = Depends(get_settings)) -> CandleCacheStore:
    """Create the persistent completed-candle cache for a chart request."""
    return CandleCacheStore(settings)


def get_upstox_service(request: Request, settings: Settings = Depends(get_settings)) -> UpstoxService:
    """Create the Upstox REST service for the current request, backed by the app-wide
    connection-pooled client (see `_lifespan`'s `upstox_http_client`) rather than a fresh
    `httpx.AsyncClient` per call -- without it, every outbound Upstox request pays a fresh TLS
    handshake, which compounds badly under concurrent load (e.g. 3 chart panes each firing several
    sequential Upstox calls at once)."""
    return UpstoxService(settings, client=request.app.state.upstox_http_client)


def get_usd_inr_service() -> UsdInrService:
    """Create the USD/INR quote service for the current request -- needs no Settings/token, since
    its source (Yahoo Finance) needs neither."""
    return UsdInrService()


def get_notification_store(settings: Settings = Depends(get_settings)) -> NotificationStore:
    """Create the SQLite-backed notification log store for a request."""
    return NotificationStore(settings)


def get_journal_store(settings: Settings = Depends(get_settings)) -> JournalStore:
    """Create the dedicated SQLite journal/context store for a request."""
    return JournalStore(settings)


def get_order_engine_ledger_store(
    settings: Settings = Depends(get_settings),
) -> OrderEngineLedgerStore:
    """Create the new order engine's server-authoritative ledger store for a request -- see
    `docs/ORDER_POSITION_OVERHAUL_DESIGN.md` §8."""
    return OrderEngineLedgerStore(settings)


def get_order_engine_lot_tracker(request: Request) -> OrderEngineLotTracker:
    """The app-lifetime `OrderEngineLotTracker` singleton (see `_lifespan`), not a fresh one --
    unlike `get_order_engine_ledger_store` above (safe to construct fresh per request, since it
    just opens the same SQLite file), this tracker's own value is its in-memory `_last_ltp` cache
    accumulated from real live ticks; a fresh instance would have never seen a tick and would
    report every open lot's live P&L as zero. Backs the new `GET .../ledger/pnl-summary` route --
    the one place a REST caller needs the *live* number, not just the ledger's own stored rows."""
    return request.app.state.order_engine_lot_tracker


def get_device_token_store(settings: Settings = Depends(get_settings)) -> DeviceTokenStore:
    """Create the device-token/push-preference store for a request."""
    return DeviceTokenStore(settings)


def get_max_loss_settings_store(settings: Settings = Depends(get_settings)) -> MaxLossSettingsStore:
    """Create the max-loss threshold store for a request."""
    return MaxLossSettingsStore(settings)


def get_notification_service(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> NotificationService:
    """Create a `NotificationService` wired to this app's already-running stream manager (and, once
    Phase 4 lands, its FCM push service) -- both live on `app.state`, set once in `app.main`'s
    lifespan, so a route reaches them through the request rather than constructing its own."""
    stream_manager = getattr(request.app.state, "stream_manager", None)
    fcm_service = getattr(request.app.state, "fcm_service", None)
    return NotificationService(settings, stream_manager=stream_manager, fcm_service=fcm_service)

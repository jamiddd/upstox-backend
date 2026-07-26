from __future__ import annotations

import asyncio

from fastapi import Depends, HTTPException, Request, status

from app.core.config import Settings, get_settings
from app.core.exceptions import TokenStoreError
from app.services.candle_cache_store import CandleCacheStore
from app.services.device_token_store import DeviceTokenStore
from app.services.max_loss_settings_store import MaxLossSettingsStore
from app.services.journal_store import JournalStore
from app.services.notification_service import NotificationService
from app.services.notification_store import NotificationStore
from app.services.oi_snapshot_store import OISnapshotStore
from app.services.signal_snapshot_store import SignalSnapshotStore
from app.services.token_store import EncryptedTokenStore
from app.services.tracked_instruments_store import TrackedInstrumentsStore
from app.services.upstox_service import UpstoxService
from app.services.usd_inr_service import UsdInrService


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


def get_signal_snapshot_store(settings: Settings = Depends(get_settings)) -> SignalSnapshotStore:
    """Create the SQLite-backed underlying-signal history store for a request."""
    return SignalSnapshotStore(settings)


def get_oi_snapshot_store(settings: Settings = Depends(get_settings)) -> OISnapshotStore:
    """Create the SQLite-backed per-strike OI snapshot store for a request."""
    return OISnapshotStore(settings)


def get_candle_cache_store(settings: Settings = Depends(get_settings)) -> CandleCacheStore:
    """Create the persistent completed-candle cache for a chart request."""
    return CandleCacheStore(settings)


def get_upstox_service(settings: Settings = Depends(get_settings)) -> UpstoxService:
    """Create the Upstox REST service for the current request."""
    return UpstoxService(settings)


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


def get_device_token_store(settings: Settings = Depends(get_settings)) -> DeviceTokenStore:
    """Create the device-token/push-preference store for a request."""
    return DeviceTokenStore(settings)


def get_max_loss_settings_store(settings: Settings = Depends(get_settings)) -> MaxLossSettingsStore:
    """Create the max-loss threshold store for a request."""
    return MaxLossSettingsStore(settings)


async def get_exit_all_lock(request: Request) -> asyncio.Lock:
    """The single lock a client-triggered flatten-all (`POST /orders/exit-all`) and the backend's
    own max_loss_watcher share, so they can never race into flattening the same still-open
    position twice (Upstox's position book doesn't always reflect a just-placed market order's
    fill instantly, so two flattens racing in close together could each see the position as still
    open and each submit their own exit). Set once on `app.state` in `app.main`'s lifespan;
    lazily created here too so a test building this app without running that lifespan still gets
    a usable lock instead of an AttributeError. `async def`, not `def` -- a plain sync dependency
    runs in FastAPI's worker threadpool, which has no event loop bound on this Python version,
    and `asyncio.Lock()` needs one at construction time.
    """
    lock = getattr(request.app.state, "exit_all_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        request.app.state.exit_all_lock = lock
    return lock


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

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from app.core.config import Settings, get_settings
from app.core.exceptions import TokenStoreError
from app.services.candle_cache_store import CandleCacheStore
from app.services.device_token_store import DeviceTokenStore
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


def get_device_token_store(settings: Settings = Depends(get_settings)) -> DeviceTokenStore:
    """Create the device-token/push-preference store for a request."""
    return DeviceTokenStore(settings)


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

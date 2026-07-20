from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.exceptions import TokenStoreError
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


def get_upstox_service(settings: Settings = Depends(get_settings)) -> UpstoxService:
    """Create the Upstox REST service for the current request."""
    return UpstoxService(settings)


def get_usd_inr_service() -> UsdInrService:
    """Create the USD/INR quote service for the current request -- needs no Settings/token, since
    its source (Yahoo Finance) needs neither."""
    return UsdInrService()

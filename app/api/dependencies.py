from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.exceptions import TokenStoreError
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService


def get_token_store(settings: Settings = Depends(get_settings)) -> EncryptedTokenStore:
    """Create the encrypted token store for the current request."""
    try:
        return EncryptedTokenStore(settings)
    except TokenStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": str(exc)},
        ) from exc


def get_upstox_service(settings: Settings = Depends(get_settings)) -> UpstoxService:
    """Create the Upstox REST service for the current request."""
    return UpstoxService(settings)

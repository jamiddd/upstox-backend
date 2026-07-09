from __future__ import annotations

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService


def get_token_store(settings: Settings = Depends(get_settings)) -> EncryptedTokenStore:
    """Create the encrypted token store for the current request."""
    return EncryptedTokenStore(settings)


def get_upstox_service(settings: Settings = Depends(get_settings)) -> UpstoxService:
    """Create the Upstox REST service for the current request."""
    return UpstoxService(settings)

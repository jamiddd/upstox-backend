from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.exceptions import AppConfigError


def require_mobile_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    app_settings: Settings = Depends(get_settings),
) -> None:
    """Require the static mobile API key on protected API routes."""
    try:
        app_settings.require_mobile_api_key()
    except AppConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": str(exc)},
        ) from exc

    if not x_api_key or not secrets.compare_digest(x_api_key, app_settings.mobile_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"status": "error", "message": "Invalid or missing API key"},
        )

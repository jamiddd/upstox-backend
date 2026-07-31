from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import Settings, get_settings
from app.core.exceptions import AppConfigError
from app.core.web_session import WEB_SESSION_COOKIE_NAME, verify_session_token


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


def require_web_session(
    request: Request,
    app_settings: Settings = Depends(get_settings),
) -> None:
    """Require a valid signed session cookie on routes serving the web client.

    Additive alongside require_mobile_api_key -- Android never sends this cookie and is
    unaffected by this dependency existing.
    """
    try:
        app_settings.require_web_session_secret()
    except AppConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"status": "error", "message": str(exc)},
        ) from exc

    token = request.cookies.get(WEB_SESSION_COOKIE_NAME)
    if not token or not verify_session_token(token, app_settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"status": "error", "message": "Missing or expired web session"},
        )


def require_mobile_or_web(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    app_settings: Settings = Depends(get_settings),
) -> None:
    """Accept either the mobile API key header or a valid web session cookie.

    For routes that must serve both Android and the web client (M1 onward) -- M0 itself only
    needs require_web_session wired end-to-end, but this is added now so M1 route work doesn't
    need to revisit app/core/security.py again.
    """
    token = request.cookies.get(WEB_SESSION_COOKIE_NAME)
    if token and app_settings.web_session_secret and verify_session_token(token, app_settings):
        return
    require_mobile_api_key(x_api_key=x_api_key, app_settings=app_settings)

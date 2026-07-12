from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies import get_token_store, get_upstox_service
from app.core.exceptions import (
    AppConfigError,
    TokenStoreError,
    UpstoxApiError,
    UpstoxAuthRequiredError,
)
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService
from app.core.security import require_mobile_api_key

public_router = APIRouter()
protected_router = APIRouter(dependencies=[Depends(require_mobile_api_key)])


@protected_router.get("/status")
def get_status() -> dict[str, str]:
    """Return a basic API status payload for the mobile client."""
    return {"status": "ready"}


@protected_router.get("/auth/login-url")
def get_login_url(
    state: Optional[str] = None,
    service: UpstoxService = Depends(get_upstox_service),
) -> dict[str, str]:
    """Return the Upstox OAuth login URL for the mobile client."""
    try:
        return {"login_url": service.build_login_url(state=state)}
    except AppConfigError as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc


@public_router.get("/auth/callback")
async def auth_callback(
    code: str,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, str]:
    """Exchange the Upstox OAuth code and persist the encrypted token."""
    try:
        token_payload = await service.exchange_code_for_token(code)
        token_store.save(token_payload)
    except (AppConfigError, TokenStoreError) as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc
    return {"status": "authenticated"}


@protected_router.get("/auth/status")
def auth_status(
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, bool]:
    """Report whether an encrypted Upstox token is available."""
    try:
        authenticated = token_store.has_token()
    except TokenStoreError as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    return {"authenticated": authenticated}


@protected_router.post("/auth/logout")
def logout(
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, str]:
    """Clear the encrypted Upstox token."""
    try:
        token_store.clear()
    except TokenStoreError as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    return {"status": "logged_out"}


@protected_router.get("/market/ltp")
async def get_ltp(
    instrument_key: str = Query(min_length=1),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return LTP market data from Upstox."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_ltp(access_token, instrument_key)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/market/quotes")
async def get_quotes(
    instrument_key: str = Query(min_length=1),
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return full market quotes from Upstox."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_quotes(access_token, instrument_key)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/portfolio/holdings")
async def get_holdings(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return long-term holdings from Upstox."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_holdings(access_token)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


@protected_router.get("/portfolio/positions")
async def get_positions(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> dict[str, Any]:
    """Return current positions from Upstox."""
    access_token = _load_access_token(token_store)
    try:
        return await service.get_positions(access_token)
    except UpstoxApiError as exc:
        raise _upstox_http_error(exc) from exc


def _load_access_token(token_store: EncryptedTokenStore) -> str:
    """Load the stored token or convert storage failures into API errors."""
    try:
        return token_store.load_access_token()
    except UpstoxAuthRequiredError as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except TokenStoreError as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc


def _http_error(status_code: int, message: str) -> HTTPException:
    """Build a normalized HTTP error response."""
    return HTTPException(
        status_code=status_code,
        detail={"status": "error", "message": message},
    )


def _upstox_http_error(exc: UpstoxApiError) -> HTTPException:
    """Build a normalized HTTP response for an Upstox API failure."""
    detail: dict[str, Any] = {
        "status": "error",
        "message": exc.message,
        "upstox_code": exc.upstox_code,
    }
    if exc.details is not None:
        detail["details"] = exc.details
    return HTTPException(status_code=exc.status_code, detail=detail)


router = APIRouter()
router.include_router(public_router)
router.include_router(protected_router)

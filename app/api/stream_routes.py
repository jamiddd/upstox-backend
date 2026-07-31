from __future__ import annotations

import asyncio
import logging
import secrets

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.core.config import Settings, get_settings
from app.core.web_session import WEB_SESSION_COOKIE_NAME, verify_session_token

logger = logging.getLogger(__name__)

router = APIRouter()

# WebSocket close codes in the 4000-4999 range are reserved for application use (RFC 6455).
_UNAUTHORIZED_CLOSE_CODE = 4401
_CLIENT_IDLE_TIMEOUT_SECONDS = 45.0


@router.websocket("/stream")
async def stream_endpoint(
    websocket: WebSocket,
    settings: Settings = Depends(get_settings),
) -> None:
    """The backend's single client-facing live-data channel -- replaces the app's old direct
    Upstox WebSocket and its 60s signals / 5s order-status REST polls (see
    `StreamConnectionManager`'s own doc comment for the full protocol).

    Authenticated with either the same static `X-API-Key` header every other route already
    requires (Android), or the web client's signed `psw_session` cookie -- checked explicitly in
    the body rather than via a `Header`/`Depends`-based dependency (as `require_mobile_api_key`
    does for REST routes) -- a WebSocket handshake needs an explicit `close()` on auth failure, not
    an `HTTPException`, for a clean rejection. Browsers cannot set a custom header on a WS upgrade
    request, but they do send cookies automatically, which is exactly why the web client needs this
    second path instead of reusing the header check unmodified. Same reasoning applies to
    `client_instance_id`/`generation` below -- Android sends them as headers, the web client sends
    them as query params instead.

    The handshake is accepted *before* checking auth, even on the rejection path: closing a
    WebSocket that was never accepted makes uvicorn's real ASGI server reject the connection at
    the HTTP level with a bare 403 and no code the client can read, rather than delivering the
    close code below as an actual WebSocket close frame. `TestClient`'s in-process WS transport
    hides this distinction (it preserves the close code either way), which is exactly why this
    needs a live-server smoke test, not just unit tests, to catch.
    """
    await websocket.accept()
    api_key = websocket.headers.get("x-api-key")
    has_valid_api_key = bool(
        settings.mobile_api_key and api_key and secrets.compare_digest(api_key, settings.mobile_api_key),
    )
    session_cookie = websocket.cookies.get(WEB_SESSION_COOKIE_NAME)
    has_valid_session = bool(
        settings.web_session_secret
        and session_cookie
        and verify_session_token(session_cookie, settings),
    )
    if not has_valid_api_key and not has_valid_session:
        await websocket.close(code=_UNAUTHORIZED_CLOSE_CODE)
        return

    manager = websocket.app.state.stream_manager
    # Browsers cannot set custom headers on a WS upgrade request (the same limitation the auth
    # cookie above exists to work around) -- the web client sends these as query params instead.
    # Query params are checked second (header wins if somehow both are present) purely because
    # the header path is Android's existing, already-proven behavior.
    client_instance_id = websocket.headers.get("x-client-instance-id") or websocket.query_params.get(
        "client_instance_id",
    )
    generation_raw = websocket.headers.get("x-connection-generation") or websocket.query_params.get(
        "generation", "0",
    )
    try:
        generation = int(generation_raw)
    except ValueError:
        generation = 0
    try:
        session = await manager.connect(
            websocket,
            client_instance_id=client_instance_id,
            generation=generation,
        )
    except ValueError:
        return
    try:
        while True:
            raw = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=_CLIENT_IDLE_TIMEOUT_SECONDS,
            )
            await manager.handle_message(session, raw)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        await manager.disconnect(session)

from __future__ import annotations

import asyncio
import logging
import secrets

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.core.config import Settings, get_settings

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

    Authenticated with the same static `X-API-Key` header every other route already requires,
    checked explicitly in the body rather than via a `Header`-based dependency (as
    `require_mobile_api_key` does for REST routes) -- a WebSocket handshake needs an explicit
    `close()` on auth failure, not an `HTTPException`, for a clean rejection.

    The handshake is accepted *before* checking the key, even on the rejection path: closing a
    WebSocket that was never accepted makes uvicorn's real ASGI server reject the connection at
    the HTTP level with a bare 403 and no code the client can read, rather than delivering the
    close code below as an actual WebSocket close frame. `TestClient`'s in-process WS transport
    hides this distinction (it preserves the close code either way), which is exactly why this
    needs a live-server smoke test, not just unit tests, to catch.
    """
    await websocket.accept()
    api_key = websocket.headers.get("x-api-key")
    if not settings.mobile_api_key or not api_key or not secrets.compare_digest(
        api_key, settings.mobile_api_key,
    ):
        await websocket.close(code=_UNAUTHORIZED_CLOSE_CODE)
        return

    manager = websocket.app.state.stream_manager
    client_instance_id = websocket.headers.get("x-client-instance-id")
    try:
        generation = int(websocket.headers.get("x-connection-generation", "0"))
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

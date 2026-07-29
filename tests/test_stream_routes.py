from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.stream_routes import router as stream_router
from app.core.config import Settings, get_settings


def _settings() -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/stream_route_test_token.enc"),
    )


class _FakeSubscriptionManager:
    async def set_client_subscription(self, session_id, *, d30, full, ltpc) -> None:
        pass

    async def remove_client(self, session_id) -> None:
        pass


class _FakeStreamManager:
    """A minimal stand-in exercising exactly the connect/handle_message/disconnect contract the
    route relies on -- lets this test focus purely on the route's own auth/wiring behavior rather
    than re-testing StreamConnectionManager's internals (already covered by
    test_stream_connection_manager.py)."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self.disconnected = False

    async def connect(self, websocket, **kwargs):
        # The route already calls websocket.accept() before reaching here (see stream_routes.py).
        return websocket

    async def handle_message(self, session, raw: str) -> None:
        self.received.append(raw)
        await session.send_text(json.dumps({"type": "ack", "data": raw}))

    async def disconnect(self, session) -> None:
        self.disconnected = True


def _app(stream_manager: _FakeStreamManager) -> FastAPI:
    app = FastAPI()
    app.include_router(stream_router, prefix="/api")
    app.dependency_overrides[get_settings] = _settings
    app.state.stream_manager = stream_manager
    return app


def test_missing_api_key_closes_connection() -> None:
    app = _app(_FakeStreamManager())
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/stream") as ws:
            ws.receive_text()  # the handshake succeeds; the close frame arrives as the first message
    assert exc_info.value.code == 4401


def test_wrong_api_key_closes_connection() -> None:
    app = _app(_FakeStreamManager())
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/stream", headers={"X-API-Key": "wrong"}) as ws:
            ws.receive_text()
    assert exc_info.value.code == 4401


def test_valid_api_key_connects_and_relays_messages() -> None:
    stream_manager = _FakeStreamManager()
    app = _app(stream_manager)
    client = TestClient(app)

    with client.websocket_connect("/api/stream", headers={"X-API-Key": "mobile-secret"}) as ws:
        ws.send_text(json.dumps({"type": "subscribe", "full": ["A"], "ltpc": []}))
        reply = ws.receive_text()

    assert json.loads(reply)["type"] == "ack"
    assert stream_manager.received == [json.dumps({"type": "subscribe", "full": ["A"], "ltpc": []})]


def test_disconnect_calls_manager_disconnect() -> None:
    stream_manager = _FakeStreamManager()
    app = _app(stream_manager)
    client = TestClient(app)

    with client.websocket_connect("/api/stream", headers={"X-API-Key": "mobile-secret"}):
        pass

    assert stream_manager.disconnected is True

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.services.upstox_ws_client import UpstoxAuthPendingError, UpstoxWebSocketClient


class _FakeConnection:
    """Minimal stand-in for a `websockets` client connection: an async context manager exposing
    `send`/`recv`/`close`, fed by a queue the test controls directly."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    async def __aenter__(self) -> "_FakeConnection":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        self.closed = True

    async def send(self, payload: Any) -> None:
        self.sent.append(payload)

    async def recv(self) -> Any:
        return await self._incoming.get()

    async def close(self) -> None:
        self.closed = True

    def push(self, message: Any) -> None:
        self._incoming.put_nowait(message)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_resubscribes_and_dispatches_messages_on_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _FakeConnection()
    monkeypatch.setattr(
        "app.services.upstox_ws_client.websockets.connect",
        lambda *a, **k: connection,
    )
    received: list[Any] = []
    client = UpstoxWebSocketClient(
        name="test",
        authorize=lambda: _ok("wss://feed.test/socket"),
        on_message=received.append,
        desired_subscriptions=lambda: [{"method": "sub", "data": {"mode": "full"}}],
    )

    task = asyncio.create_task(client._connect_once())
    await asyncio.sleep(0.05)  # let the connect+resubscribe sequence run

    assert connection.sent == [json.dumps({"method": "sub", "data": {"mode": "full"}}).encode("utf-8")]

    connection.push(b"tick-1")
    connection.push(b"tick-2")
    await asyncio.sleep(0.05)
    assert received == [b"tick-1", b"tick-2"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_stale_connection_triggers_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _FakeConnection()
    monkeypatch.setattr(
        "app.services.upstox_ws_client.websockets.connect",
        lambda *a, **k: connection,
    )
    client = UpstoxWebSocketClient(
        name="test",
        authorize=lambda: _ok("wss://feed.test/socket"),
        on_message=lambda _msg: None,
        stale_after_seconds=0.05,
    )

    auth_pending = await client._connect_once()

    assert auth_pending is False
    assert client._connection is None  # cleaned up after the stale timeout forced a return


@pytest.mark.anyio
async def test_stale_watchdog_disabled_waits_indefinitely_for_a_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`stale_after_seconds=None` (as `UpstoxPortfolioFeedClient` passes) must not apply any
    timeout at all -- a purely event-driven feed going quiet is normal, not a sign of staleness."""
    connection = _FakeConnection()
    monkeypatch.setattr(
        "app.services.upstox_ws_client.websockets.connect",
        lambda *a, **k: connection,
    )
    received: list[Any] = []
    client = UpstoxWebSocketClient(
        name="test",
        authorize=lambda: _ok("wss://feed.test/socket"),
        on_message=received.append,
        stale_after_seconds=None,
    )

    task = asyncio.create_task(client._connect_once())
    await asyncio.sleep(0.05)
    assert client._connection is not None  # still connected, no timeout fired

    connection.push(b"order-update")
    await asyncio.sleep(0.05)
    assert received == [b"order-update"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_auth_pending_is_reported_distinctly(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise() -> str:
        raise UpstoxAuthPendingError("no token yet")

    client = UpstoxWebSocketClient(name="test", authorize=_raise, on_message=lambda _msg: None)

    auth_pending = await client._connect_once()

    assert auth_pending is True


@pytest.mark.anyio
async def test_send_json_encodes_as_binary_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstox silently ignores TEXT-frame control messages -- send_json must always encode to
    bytes (a BINARY frame), never a bare str (which the `websockets` library sends as TEXT)."""
    connection = _FakeConnection()
    client = UpstoxWebSocketClient(name="test", authorize=lambda: _ok(""), on_message=lambda _msg: None)
    client._connection = connection

    await client.send_json({"method": "sub"})

    assert connection.sent == [json.dumps({"method": "sub"}).encode("utf-8")]
    assert isinstance(connection.sent[0], bytes)


@pytest.mark.anyio
async def test_send_json_is_a_noop_when_not_connected() -> None:
    client = UpstoxWebSocketClient(name="test", authorize=lambda: _ok(""), on_message=lambda _msg: None)
    await client.send_json({"method": "sub"})  # must not raise


@pytest.mark.anyio
async def test_force_reconnect_closes_current_socket_and_advances_generation() -> None:
    connection = _FakeConnection()
    client = UpstoxWebSocketClient(name="test", authorize=lambda: _ok(""), on_message=lambda _msg: None)
    client._connection = connection
    generation = client._generation

    await client.force_reconnect("D30 stalled")

    assert connection.closed is True
    assert client._connection is None
    assert client._generation == generation + 1


async def _ok(url: str) -> str:
    return url

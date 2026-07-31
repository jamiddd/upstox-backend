from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketState

from app.core.config import Settings
from app.core.exceptions import UpstoxApiError
from app.services import stream_connection_manager as stream_connection_manager_module
from app.services.stream_connection_manager import StreamConnectionManager
from app.services.upstox_market_feed_client import FeedCandle, FeedTick, MarketDepthLevel


def _settings(tmp_path: Path | None = None) -> Settings:
    base = tmp_path or Path("/tmp")
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=base / "stream_test_token.enc",
        oi_database_path=base / "oi.sqlite3",
    )


class _FakeWebSocket:
    def __init__(self) -> None:
        self.application_state = WebSocketState.CONNECTED
        self.sent: list[dict[str, Any]] = []
        self.closed: list[tuple[int, str]] = []

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int, reason: str) -> None:
        self.closed.append((code, reason))


class _FakeSubscriptionManager:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, list[str], list[str], list[str]]] = []
        self.removed: list[str] = []

    async def set_client_subscription(
        self, session_id: str, *, d30: list[str], full: list[str], ltpc: list[str],
    ) -> None:
        self.set_calls.append((session_id, d30, full, ltpc))

    async def remove_client(self, session_id: str) -> None:
        self.removed.append(session_id)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _manager() -> tuple[StreamConnectionManager, _FakeSubscriptionManager]:
    fake_sub = _FakeSubscriptionManager()
    manager = StreamConnectionManager(settings=_settings(), subscription_manager=fake_sub)
    return manager, fake_sub


@pytest.mark.anyio
async def test_subscribe_message_forwards_to_subscription_manager() -> None:
    manager, fake_sub = _manager()
    websocket = _FakeWebSocket()
    session = await manager.connect(websocket)  # type: ignore[arg-type]

    await manager.handle_message(
        session,
        json.dumps({
            "type": "subscribe",
            "revision": 1,
            "d30": ["D"],
            "full": ["A", "B"],
            "ltpc": ["C"],
        }),
    )

    assert fake_sub.set_calls == [(session.session_id, ["D"], ["A", "B"], ["C"])]
    assert session.d30_keys == {"D"}
    assert session.full_keys == {"A", "B"}
    assert session.ltpc_keys == {"C"}


@pytest.mark.anyio
async def test_newer_generation_retires_older_session_for_same_client() -> None:
    manager, fake_sub = _manager()
    old_socket = _FakeWebSocket()
    new_socket = _FakeWebSocket()
    old_session = await manager.connect(  # type: ignore[arg-type]
        old_socket, client_instance_id="phone", generation=1,
    )

    new_session = await manager.connect(  # type: ignore[arg-type]
        new_socket, client_instance_id="phone", generation=2,
    )

    assert old_session.session_id in fake_sub.removed
    assert old_socket.closed == [(4001, "Replaced by newer connection")]
    assert new_session.client_instance_id == "phone"
    assert new_session.generation == 2
@pytest.mark.anyio
async def test_stale_subscription_revision_is_ignored() -> None:
    manager, fake_sub = _manager()
    session = await manager.connect(_FakeWebSocket())  # type: ignore[arg-type]

    await manager.handle_message(
        session, json.dumps({"type": "subscribe", "revision": 2, "d30": ["NEW"]}),
    )
    await manager.handle_message(
        session, json.dumps({"type": "subscribe", "revision": 1, "d30": ["OLD"]}),
    )

    assert fake_sub.set_calls == [(session.session_id, ["NEW"], [], [])]

    snapshot = manager.debug_snapshot()
    assert snapshot["active_session_count"] == 1
    assert snapshot["sessions"][0]["client_instance_id"] == session.client_instance_id
    assert snapshot["sessions"][0]["d30_count"] == 1


@pytest.mark.anyio
async def test_malformed_message_is_ignored() -> None:
    manager, fake_sub = _manager()
    websocket = _FakeWebSocket()
    session = await manager.connect(websocket)  # type: ignore[arg-type]

    await manager.handle_message(session, "not json")
    await manager.handle_message(session, json.dumps(["not", "a", "dict"]))
    await manager.handle_message(session, json.dumps({"type": "unknown_type"}))

    assert fake_sub.set_calls == []


@pytest.mark.anyio
async def test_set_underlying_records_session_state_and_starts_signals_task() -> None:
    manager, _ = _manager()
    websocket = _FakeWebSocket()
    session = await manager.connect(websocket)  # type: ignore[arg-type]

    await manager.handle_message(
        session,
        json.dumps({
            "type": "set_underlying",
            "underlying_key": "NSE_INDEX|Nifty 50",
            "expiry_date": "2026-07-31",
            "underlying_symbol": "NIFTY",
        }),
    )

    assert session.underlying_key == "NSE_INDEX|Nifty 50"
    assert session.expiry_date == "2026-07-31"
    assert session.underlying_symbol == "NIFTY"
    assert session.signals_task is not None

    await manager.disconnect(session)


@pytest.mark.anyio
async def test_disconnect_cancels_signals_task_and_removes_client() -> None:
    manager, fake_sub = _manager()
    websocket = _FakeWebSocket()
    session = await manager.connect(websocket)  # type: ignore[arg-type]
    await manager.handle_message(
        session, json.dumps({"type": "set_underlying", "underlying_key": "NSE_INDEX|Nifty 50"}),
    )
    task = session.signals_task
    assert task is not None

    await manager.disconnect(session)
    await asyncio.sleep(0.01)

    assert task.cancelled() or task.done()
    assert fake_sub.removed == [session.session_id]


@pytest.mark.anyio
async def test_dispatch_tick_only_reaches_sessions_watching_that_instrument() -> None:
    manager, _ = _manager()
    watching = _FakeWebSocket()
    not_watching = _FakeWebSocket()
    watching_session = await manager.connect(watching)  # type: ignore[arg-type]
    not_watching_session = await manager.connect(not_watching)  # type: ignore[arg-type]
    watching_session.full_keys = {"NSE_FO|111"}

    tick = FeedTick(
        instrument_key="NSE_FO|111",
        ltp=125.5,
        last_trade_time_millis=1_700_000_000_000,
        bid_price=125.0,
        ask_price=126.0,
        market_depth=(MarketDepthLevel(450, 125.0, 300, 126.0),),
        total_bid_quantity=93_950,
        total_ask_quantity=116_950,
        one_minute_candle=FeedCandle(
            timestamp_millis=1_700_000_000_000, open=124.0, high=126.0, low=123.0, close=125.5, volume=900,
        ),
    )
    await manager.dispatch_tick(tick)

    assert len(watching.sent) == 1
    assert watching.sent[0]["type"] == "tick"
    assert watching.sent[0]["data"]["instrument_key"] == "NSE_FO|111"
    assert watching.sent[0]["data"]["market_depth"] == [{
        "bid_quantity": 450,
        "bid_price": 125.0,
        "ask_quantity": 300,
        "ask_price": 126.0,
    }]
    assert watching.sent[0]["data"]["total_bid_quantity"] == 93_950
    assert watching.sent[0]["data"]["total_ask_quantity"] == 116_950
    assert watching.sent[0]["data"]["one_minute_candle"]["close"] == 125.5
    assert not_watching.sent == []
    del not_watching_session


@pytest.mark.anyio
async def test_dispatch_order_flow_only_reaches_d30_subscribed_sessions() -> None:
    """Unlike dispatch_tick (any tier), order_flow is d30-only -- the analysis needs full depth
    and is meaningless for full/ltpc-tier subscribers."""
    manager, _ = _manager()
    d30_session_socket = _FakeWebSocket()
    full_session_socket = _FakeWebSocket()
    d30_session = await manager.connect(d30_session_socket)  # type: ignore[arg-type]
    full_session = await manager.connect(full_session_socket)  # type: ignore[arg-type]
    d30_session.d30_keys = {"NSE_FO|111"}
    full_session.full_keys = {"NSE_FO|111"}

    await manager.dispatch_order_flow("NSE_FO|111", {"ofi_rolling": 42.0})

    assert d30_session_socket.sent == [
        {"type": "order_flow", "data": {"instrument_key": "NSE_FO|111", "ofi_rolling": 42.0}},
    ]
    assert full_session_socket.sent == []


@pytest.mark.anyio
async def test_dispatch_order_update_reaches_every_connected_session() -> None:
    manager, _ = _manager()
    a = _FakeWebSocket()
    b = _FakeWebSocket()
    await manager.connect(a)  # type: ignore[arg-type]
    await manager.connect(b)  # type: ignore[arg-type]

    await manager.dispatch_order_update({"order_id": "O1", "status": "complete"})

    assert a.sent == [{"type": "order_update", "data": {"order_id": "O1", "status": "complete"}}]
    assert b.sent == a.sent


@pytest.mark.anyio
async def test_dispatch_notification_reaches_every_connected_session() -> None:
    manager, _ = _manager()
    a = _FakeWebSocket()
    b = _FakeWebSocket()
    await manager.connect(a)  # type: ignore[arg-type]
    await manager.connect(b)  # type: ignore[arg-type]
    notification = {"id": 1, "category": "auth", "severity": "critical", "title": "t", "message": "m"}

    await manager.dispatch_notification(notification)

    assert a.sent == [{"type": "notification", "data": notification}]
    assert b.sent == a.sent


class _FakeTokenStoreWithToken:
    def __init__(self, settings: Settings) -> None:
        pass

    def load_access_token(self) -> str:
        return "token"


class _AlwaysFailingSignalsService:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def get_signals(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise UpstoxApiError("boom", status_code=500, upstox_code=None)


class _FakeNotificationService:
    def __init__(self) -> None:
        self.recorded: list[dict[str, object]] = []

    async def record(self, **kwargs: object) -> dict[str, object]:
        self.recorded.append(kwargs)
        return {"id": len(self.recorded), **kwargs}


@pytest.mark.anyio
async def test_push_signals_once_notifies_after_threshold_consecutive_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_connection_manager_module, "EncryptedTokenStore", _FakeTokenStoreWithToken)
    monkeypatch.setattr(
        stream_connection_manager_module, "UnderlyingSignalsService", _AlwaysFailingSignalsService,
    )
    notification_service = _FakeNotificationService()
    fake_sub = _FakeSubscriptionManager()
    manager = StreamConnectionManager(
        settings=_settings(tmp_path), subscription_manager=fake_sub, notification_service=notification_service,
    )
    websocket = _FakeWebSocket()
    session = await manager.connect(websocket)  # type: ignore[arg-type]
    session.underlying_key = "NSE_INDEX|Nifty 50"

    await manager._push_signals_once(session)
    await manager._push_signals_once(session)
    assert notification_service.recorded == []

    await manager._push_signals_once(session)
    assert len(notification_service.recorded) == 1
    assert notification_service.recorded[0]["severity"] == "warning"

    # Stays silent while still failing -- one notification per failure episode, not one per tick.
    await manager._push_signals_once(session)
    assert len(notification_service.recorded) == 1


@pytest.mark.anyio
async def test_send_is_a_noop_when_socket_not_connected() -> None:
    manager, _ = _manager()
    websocket = _FakeWebSocket()
    websocket.application_state = WebSocketState.DISCONNECTED
    session = await manager.connect(websocket)  # type: ignore[arg-type]

    await manager.dispatch_order_update({"order_id": "O1", "status": "complete"})

    assert websocket.sent == []

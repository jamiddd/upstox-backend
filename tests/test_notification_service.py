from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.notification_service import NotificationService


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="k",
        upstox_api_secret="s",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=tmp_path / "token.enc",
        notification_database_path=tmp_path / "notifications.sqlite3",
        device_token_path=tmp_path / "device_token.json",
    )


class _FakeStreamManager:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    async def dispatch_notification(self, notification: dict[str, Any]) -> None:
        self.dispatched.append(notification)


class _RaisingStreamManager:
    async def dispatch_notification(self, notification: dict[str, Any]) -> None:
        raise RuntimeError("stream is down")


class _FakeFcmService:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, token: str, *, title: str, body: str, data: dict[str, str]) -> None:
        self.sent.append({"token": token, "title": title, "body": body, "data": data})


class _RaisingFcmService:
    async def send(self, token: str, *, title: str, body: str, data: dict[str, str]) -> None:
        raise RuntimeError("FCM is down")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_record_persists_and_dispatches_over_the_stream(tmp_path: Path) -> None:
    stream_manager = _FakeStreamManager()
    service = NotificationService(_settings(tmp_path), stream_manager=stream_manager)

    notification = await service.record(
        category="auth", severity="critical", title="Login expired", message="Please re-login.",
    )

    assert notification["title"] == "Login expired"
    assert stream_manager.dispatched == [notification]
    items, _ = service.store.list_notifications()
    assert items[0]["title"] == "Login expired"


@pytest.mark.anyio
async def test_record_works_without_a_stream_manager(tmp_path: Path) -> None:
    service = NotificationService(_settings(tmp_path))

    notification = await service.record(category="auth", severity="info", title="a", message="m")

    assert notification["id"] > 0


@pytest.mark.anyio
async def test_stream_dispatch_failure_does_not_prevent_persistence(tmp_path: Path) -> None:
    service = NotificationService(_settings(tmp_path), stream_manager=_RaisingStreamManager())

    notification = await service.record(category="auth", severity="info", title="a", message="m")

    items, _ = service.store.list_notifications()
    assert items[0]["id"] == notification["id"]


@pytest.mark.anyio
async def test_pushes_when_severity_meets_device_preference(tmp_path: Path) -> None:
    fcm_service = _FakeFcmService()
    service = NotificationService(_settings(tmp_path), fcm_service=fcm_service)
    service.device_token_store.save(fcm_token="device-token", push_preference="critical")

    await service.record(category="auth", severity="critical", title="Login expired", message="Please re-login.")

    assert len(fcm_service.sent) == 1
    assert fcm_service.sent[0]["token"] == "device-token"
    assert fcm_service.sent[0]["title"] == "Login expired"


@pytest.mark.anyio
async def test_does_not_push_below_device_preference_threshold(tmp_path: Path) -> None:
    fcm_service = _FakeFcmService()
    service = NotificationService(_settings(tmp_path), fcm_service=fcm_service)
    service.device_token_store.save(fcm_token="device-token", push_preference="critical")

    await service.record(category="orders", severity="info", title="Order filled", message="m")

    assert fcm_service.sent == []


@pytest.mark.anyio
async def test_does_not_push_when_preference_is_off(tmp_path: Path) -> None:
    fcm_service = _FakeFcmService()
    service = NotificationService(_settings(tmp_path), fcm_service=fcm_service)
    service.device_token_store.save(fcm_token="device-token", push_preference="off")

    await service.record(category="auth", severity="critical", title="a", message="m")

    assert fcm_service.sent == []


@pytest.mark.anyio
async def test_does_not_push_when_no_device_registered(tmp_path: Path) -> None:
    fcm_service = _FakeFcmService()
    service = NotificationService(_settings(tmp_path), fcm_service=fcm_service)

    await service.record(category="auth", severity="critical", title="a", message="m")

    assert fcm_service.sent == []


@pytest.mark.anyio
async def test_push_failure_does_not_raise(tmp_path: Path) -> None:
    service = NotificationService(_settings(tmp_path), fcm_service=_RaisingFcmService())
    service.device_token_store.save(fcm_token="device-token", push_preference="everything")

    # Must not raise even though the FCM double always fails.
    notification = await service.record(category="auth", severity="info", title="a", message="m")

    assert notification["id"] > 0

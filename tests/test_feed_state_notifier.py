from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.main import _FeedStateNotifier


class _FakeNotificationService:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append(kwargs)
        return {"id": len(self.recorded), **kwargs}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_notifies_only_after_threshold_consecutive_failures() -> None:
    notification_service = _FakeNotificationService()
    notifier = _FeedStateNotifier(name="Market data feed", notification_service=notification_service)

    notifier.handle("disconnected")
    notifier.handle("disconnected")
    await asyncio.sleep(0)
    assert notification_service.recorded == []

    notifier.handle("disconnected")
    await asyncio.sleep(0)
    assert len(notification_service.recorded) == 1
    assert notification_service.recorded[0]["severity"] == "warning"


@pytest.mark.anyio
async def test_does_not_notify_again_while_still_failing() -> None:
    notification_service = _FakeNotificationService()
    notifier = _FeedStateNotifier(name="Market data feed", notification_service=notification_service)

    for _ in range(10):
        notifier.handle("disconnected")
    await asyncio.sleep(0)

    assert len(notification_service.recorded) == 1


@pytest.mark.anyio
async def test_reconnect_after_notified_failure_sends_a_recovery_notification() -> None:
    notification_service = _FakeNotificationService()
    notifier = _FeedStateNotifier(name="Market data feed", notification_service=notification_service)

    for _ in range(3):
        notifier.handle("disconnected")
    notifier.handle("connected")
    await asyncio.sleep(0)

    assert len(notification_service.recorded) == 2
    assert notification_service.recorded[1]["severity"] == "info"
    assert "reconnected" in notification_service.recorded[1]["title"]


@pytest.mark.anyio
async def test_reconnect_without_a_prior_notification_stays_silent() -> None:
    notification_service = _FakeNotificationService()
    notifier = _FeedStateNotifier(name="Market data feed", notification_service=notification_service)

    notifier.handle("disconnected")
    notifier.handle("connected")
    await asyncio.sleep(0)

    assert notification_service.recorded == []


@pytest.mark.anyio
async def test_auth_pending_uses_a_distinct_message() -> None:
    notification_service = _FakeNotificationService()
    notifier = _FeedStateNotifier(name="Portfolio feed", notification_service=notification_service)

    for _ in range(3):
        notifier.handle("auth_pending")
    await asyncio.sleep(0)

    assert "login" in notification_service.recorded[0]["message"]

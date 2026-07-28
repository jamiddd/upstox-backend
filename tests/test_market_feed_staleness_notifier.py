from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.main import _MarketFeedStalenessNotifier


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
async def test_notifies_only_after_threshold_consecutive_nudges() -> None:
    notification_service = _FakeNotificationService()
    notifier = _MarketFeedStalenessNotifier(notification_service=notification_service)

    notifier.handle(["NSE_FO|SOME_KEY"])
    notifier.handle(["NSE_FO|SOME_KEY"])
    await asyncio.sleep(0)
    assert notification_service.recorded == []

    notifier.handle(["NSE_FO|SOME_KEY"])
    await asyncio.sleep(0)
    assert len(notification_service.recorded) == 1
    assert notification_service.recorded[0]["severity"] == "warning"
    assert "NSE_FO|SOME_KEY" in notification_service.recorded[0]["message"]


@pytest.mark.anyio
async def test_does_not_notify_again_while_still_stale() -> None:
    notification_service = _FakeNotificationService()
    notifier = _MarketFeedStalenessNotifier(notification_service=notification_service)

    for _ in range(10):
        notifier.handle(["NSE_FO|SOME_KEY"])
    await asyncio.sleep(0)

    assert len(notification_service.recorded) == 1


@pytest.mark.anyio
async def test_recovery_after_notified_staleness_sends_a_recovery_notification() -> None:
    notification_service = _FakeNotificationService()
    notifier = _MarketFeedStalenessNotifier(notification_service=notification_service)

    for _ in range(3):
        notifier.handle(["NSE_FO|SOME_KEY"])
    notifier.handle([])
    await asyncio.sleep(0)

    assert len(notification_service.recorded) == 2
    assert notification_service.recorded[1]["severity"] == "info"
    assert "recovered" in notification_service.recorded[1]["title"]


@pytest.mark.anyio
async def test_recovery_without_a_prior_notification_stays_silent() -> None:
    notification_service = _FakeNotificationService()
    notifier = _MarketFeedStalenessNotifier(notification_service=notification_service)

    notifier.handle(["NSE_FO|SOME_KEY"])
    notifier.handle([])
    await asyncio.sleep(0)

    assert notification_service.recorded == []


@pytest.mark.anyio
async def test_tracks_multiple_keys_independently() -> None:
    notification_service = _FakeNotificationService()
    notifier = _MarketFeedStalenessNotifier(notification_service=notification_service)

    # Each handle() call represents one staleness-check pass with the *complete* currently-nudged
    # set for that pass -- KEY_A stays nudged throughout so it never looks "recovered" partway
    # through, while KEY_B joins a pass later and crosses the threshold on its own schedule.
    notifier.handle(["KEY_A"])
    notifier.handle(["KEY_A", "KEY_B"])
    notifier.handle(["KEY_A", "KEY_B"])
    notifier.handle(["KEY_A", "KEY_B"])
    await asyncio.sleep(0)

    assert len(notification_service.recorded) == 2
    assert all(record["severity"] == "warning" for record in notification_service.recorded)

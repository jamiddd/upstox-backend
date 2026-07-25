from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services import notification_retention
from app.services.notification_retention import run_notification_retention


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
        notification_retention_days=30,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_runs_cleanup_with_configured_cutoff_once_per_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cutoffs = []

    class FakeStore:
        def __init__(self, settings: Settings) -> None:
            pass

        def delete_expired_before(self, cutoff):
            cutoffs.append(cutoff)
            return 2

    monkeypatch.setattr(notification_retention, "NotificationStore", FakeStore)
    monkeypatch.setattr(notification_retention, "_CHECK_INTERVAL_SECONDS", 0.0)
    moment = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)

    task = asyncio.create_task(run_notification_retention(_settings(tmp_path), now=lambda: moment))
    for _ in range(4):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cutoffs == [moment.date().replace(day=25) - notification_retention.timedelta(days=30)]


@pytest.mark.anyio
async def test_cleanup_failure_does_not_kill_background_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class FailingStore:
        def __init__(self, settings: Settings) -> None:
            pass

        def delete_expired_before(self, cutoff):
            nonlocal attempts
            attempts += 1
            raise OSError("disk unavailable")

    monkeypatch.setattr(notification_retention, "NotificationStore", FailingStore)
    monkeypatch.setattr(notification_retention, "_CHECK_INTERVAL_SECONDS", 0.0)

    task = asyncio.create_task(run_notification_retention(_settings(tmp_path)))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts >= 1

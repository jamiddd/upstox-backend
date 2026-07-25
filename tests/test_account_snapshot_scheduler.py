from __future__ import annotations

import asyncio
from datetime import datetime as RealDateTime
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import UpstoxApiError
from app.services import account_snapshot_scheduler
from app.services.account_snapshot_scheduler import run_account_snapshot_scheduler


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="k",
        upstox_api_secret="s",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=tmp_path / "token.enc",
        account_snapshot_path=tmp_path / "account.json",
    )


class FakeNotifications:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append(kwargs)
        return kwargs


class FixedDateTime:
    @classmethod
    def now(cls, zone):
        return RealDateTime(2026, 7, 25, 23, 5, tzinfo=zone)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_successful_snapshot_records_info_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = []

    class FakeStore:
        def __init__(self, settings: Settings) -> None:
            pass

        def load(self):
            return saved[-1] if saved else None

        def save(self, snapshot):
            saved.append(snapshot)

    class FakeTokenStore:
        def __init__(self, settings: Settings) -> None:
            pass

        def has_token(self) -> bool:
            return True

        def load_access_token(self) -> str:
            return "token"

    class FakeService:
        def __init__(self, upstox) -> None:
            pass

        async def summary(self, access_token: str):
            return {
                "available_margin": 100_000,
                "margin_used": 2_500,
                "closing_balance": 0,
                "funds_unavailable_note": None,
            }

    monkeypatch.setattr(account_snapshot_scheduler, "AccountSnapshotStore", FakeStore)
    monkeypatch.setattr(account_snapshot_scheduler, "EncryptedTokenStore", FakeTokenStore)
    monkeypatch.setattr(account_snapshot_scheduler, "MainScreenService", FakeService)
    monkeypatch.setattr(account_snapshot_scheduler, "UpstoxService", lambda settings: object())
    monkeypatch.setattr(account_snapshot_scheduler, "datetime", FixedDateTime)
    monkeypatch.setattr(account_snapshot_scheduler, "_POLL_SECONDS", 0.0)
    notifications = FakeNotifications()

    task = asyncio.create_task(run_account_snapshot_scheduler(_settings(tmp_path), notifications))
    for _ in range(6):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(saved) == 1
    assert saved[0].estimated_balance == 102_500
    assert notifications.recorded == [{
        "category": "account",
        "severity": "info",
        "title": "Account snapshot captured",
        "message": "Estimated balance of 102500.00 recorded for 2026-07-25.",
    }]


@pytest.mark.anyio
async def test_repeated_snapshot_failure_notifies_only_once_per_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        def __init__(self, settings: Settings) -> None:
            pass

        def load(self):
            return None

    class FakeTokenStore:
        def __init__(self, settings: Settings) -> None:
            pass

        def has_token(self) -> bool:
            return True

        def load_access_token(self) -> str:
            return "token"

    class FailingService:
        def __init__(self, upstox) -> None:
            pass

        async def summary(self, access_token: str):
            raise UpstoxApiError("unavailable")

    monkeypatch.setattr(account_snapshot_scheduler, "AccountSnapshotStore", FakeStore)
    monkeypatch.setattr(account_snapshot_scheduler, "EncryptedTokenStore", FakeTokenStore)
    monkeypatch.setattr(account_snapshot_scheduler, "MainScreenService", FailingService)
    monkeypatch.setattr(account_snapshot_scheduler, "UpstoxService", lambda settings: object())
    monkeypatch.setattr(account_snapshot_scheduler, "datetime", FixedDateTime)
    monkeypatch.setattr(account_snapshot_scheduler, "_POLL_SECONDS", 0.0)
    notifications = FakeNotifications()

    task = asyncio.create_task(run_account_snapshot_scheduler(_settings(tmp_path), notifications))
    for _ in range(8):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(notifications.recorded) == 1
    assert notifications.recorded[0]["severity"] == "warning"

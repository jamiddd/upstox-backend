from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services import auth_watchdog
from app.services.auth_watchdog import run_auth_watchdog


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="k",
        upstox_api_secret="s",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=tmp_path / "token.enc",
    )


class _FakeNotificationService:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append(kwargs)
        return {"id": len(self.recorded), **kwargs}


class _ScriptedTokenStore:
    """Reports authenticated/unauthenticated on successive calls per a scripted sequence."""

    def __init__(self, script: list[bool]) -> None:
        self._script = list(script)

    def has_token(self) -> bool:
        return True

    def load_access_token(self) -> str:
        from app.core.exceptions import UpstoxAuthRequiredError

        is_authenticated = self._script.pop(0) if self._script else self._script[-1]
        if not is_authenticated:
            raise UpstoxAuthRequiredError("expired")
        return "token"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_notifies_once_on_transition_from_authenticated_to_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    notification_service = _FakeNotificationService()
    token_store = _ScriptedTokenStore([True, True, False, False, False])
    monkeypatch.setattr(auth_watchdog, "EncryptedTokenStore", lambda settings: token_store)
    monkeypatch.setattr(auth_watchdog, "_POLL_SECONDS", 0.0)

    task = asyncio.create_task(run_auth_watchdog(_settings(tmp_path), notification_service))
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(notification_service.recorded) == 1
    assert notification_service.recorded[0]["category"] == "auth"
    assert notification_service.recorded[0]["severity"] == "critical"


@pytest.mark.anyio
async def test_does_not_notify_while_continuously_authenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    notification_service = _FakeNotificationService()
    token_store = _ScriptedTokenStore([True, True, True])
    monkeypatch.setattr(auth_watchdog, "EncryptedTokenStore", lambda settings: token_store)
    monkeypatch.setattr(auth_watchdog, "_POLL_SECONDS", 0.0)

    task = asyncio.create_task(run_auth_watchdog(_settings(tmp_path), notification_service))
    for _ in range(3):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert notification_service.recorded == []

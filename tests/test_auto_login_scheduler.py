from __future__ import annotations

import asyncio
from datetime import datetime as RealDateTime
from pathlib import Path
from typing import Any

import pytest

from app.core.exceptions import UpstoxAutoLoginError
from app.services import auto_login_scheduler
from app.services.auto_login_scheduler import run_auto_login_scheduler
from app.services.auto_login_state_store import AutoLoginAttemptState


class FakeNotifications:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append(kwargs)
        return kwargs


class FakeJournalReconciler:
    def __init__(self) -> None:
        self.reconcile_calls = 0

    async def reconcile(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.reconcile_calls += 1
        return {"status": "current"}


class FakeTokenStore:
    def __init__(self, settings: Any) -> None:
        self.saved: list[dict[str, Any]] = []
        self._has_token = True

    def has_token(self) -> bool:
        return self._has_token

    def save(self, payload: dict[str, Any]) -> None:
        self.saved.append(payload)
        self._has_token = True


class FakeStateStore:
    def __init__(self, settings: Any) -> None:
        self._state: AutoLoginAttemptState | None = None

    def load(self) -> AutoLoginAttemptState | None:
        return self._state

    def save(self, state: AutoLoginAttemptState) -> None:
        self._state = state


class FakeLoginService:
    def __init__(self, settings: Any, upstox_service: Any) -> None:
        pass

    def __new__(cls, *args: Any, **kwargs: Any) -> "FakeLoginService":
        return _shared_login_service


class _ScriptedLoginService:
    """Shared across the module-level FakeLoginService.__new__ trick above -- every scheduler tick
    constructs a *new* UpstoxTotpLoginService instance, but a test needs one persistent object to
    script outcomes across ticks and count calls."""

    def __init__(self, outcomes: list[Exception | dict[str, Any]]) -> None:
        self._outcomes = list(outcomes)
        self.call_count = 0

    async def login(self) -> dict[str, Any]:
        self.call_count += 1
        outcome = self._outcomes.pop(0) if self._outcomes else self._outcomes_default()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _outcomes_default(self) -> dict[str, Any]:
        raise UpstoxAutoLoginError("no more scripted outcomes")


_shared_login_service: _ScriptedLoginService


class FixedDateTime:
    _now: RealDateTime

    @classmethod
    def now(cls, zone: Any) -> RealDateTime:
        return cls._now.astimezone(zone) if cls._now.tzinfo else cls._now.replace(tzinfo=zone)


def _settings(tmp_path: Path) -> Any:
    from app.core.config import Settings

    return Settings(
        upstox_api_key="k", upstox_api_secret="s",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox", mobile_api_key="mobile-secret",
        token_encryption_key="", token_store_path=tmp_path / "token.enc",
        upstox_totp_username="9999999999", upstox_totp_secret="JBSWY3DPEHPK3PXP",
        upstox_totp_pin="1234",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _patch_common(monkeypatch: pytest.MonkeyPatch, *, hour: int, minute: int) -> None:
    FixedDateTime._now = RealDateTime(2026, 7, 27, hour, minute, tzinfo=None)
    monkeypatch.setattr(auto_login_scheduler, "datetime", FixedDateTime)
    monkeypatch.setattr(auto_login_scheduler, "AutoLoginStateStore", FakeStateStore)
    monkeypatch.setattr(auto_login_scheduler, "EncryptedTokenStore", FakeTokenStore)
    monkeypatch.setattr(auto_login_scheduler, "UpstoxService", lambda settings: object())
    monkeypatch.setattr(auto_login_scheduler, "UpstoxTotpLoginService", FakeLoginService)
    monkeypatch.setattr(auto_login_scheduler, "_POLL_SECONDS", 0.0)


async def _run_briefly(settings: Any, notifications: FakeNotifications, reconciler: FakeJournalReconciler, ticks: int) -> None:
    task = asyncio.create_task(run_auto_login_scheduler(settings, notifications, reconciler))
    for _ in range(ticks):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_successful_login_saves_token_and_notifies_and_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    global _shared_login_service
    _shared_login_service = _ScriptedLoginService([{"access_token": "fresh-token"}])
    _patch_common(monkeypatch, hour=5, minute=40)  # past the 05:35 attempt hour
    notifications = FakeNotifications()
    reconciler = FakeJournalReconciler()

    await _run_briefly(_settings(tmp_path), notifications, reconciler, ticks=4)

    assert _shared_login_service.call_count == 1
    assert reconciler.reconcile_calls == 1
    assert [n for n in notifications.recorded if n["severity"] == "info"]
    assert not [n for n in notifications.recorded if n["severity"] == "critical"]


@pytest.mark.anyio
async def test_does_not_reattempt_after_already_succeeded_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    global _shared_login_service
    _shared_login_service = _ScriptedLoginService(
        [{"access_token": "first"}, {"access_token": "should-not-be-used"}],
    )
    _patch_common(monkeypatch, hour=6, minute=0)
    notifications = FakeNotifications()
    reconciler = FakeJournalReconciler()

    await _run_briefly(_settings(tmp_path), notifications, reconciler, ticks=6)

    # Only the first tick's attempt should ever fire -- every later tick sees already_succeeded_today.
    assert _shared_login_service.call_count == 1


@pytest.mark.anyio
async def test_stops_after_max_attempts_and_notifies_critical_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    global _shared_login_service
    _shared_login_service = _ScriptedLoginService(
        [UpstoxAutoLoginError("boom")] * auto_login_scheduler._MAX_ATTEMPTS_PER_DAY,
    )
    _patch_common(monkeypatch, hour=6, minute=0)
    notifications = FakeNotifications()
    reconciler = FakeJournalReconciler()

    # One tick per attempt, plus a couple extra to confirm it truly stops at the cap.
    await _run_briefly(
        _settings(tmp_path), notifications, reconciler,
        ticks=auto_login_scheduler._MAX_ATTEMPTS_PER_DAY + 3,
    )

    assert _shared_login_service.call_count == auto_login_scheduler._MAX_ATTEMPTS_PER_DAY
    critical = [n for n in notifications.recorded if n["severity"] == "critical"]
    assert len(critical) == 1
    assert reconciler.reconcile_calls == 0


@pytest.mark.anyio
async def test_does_not_attempt_before_scheduled_hour_when_token_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    global _shared_login_service
    _shared_login_service = _ScriptedLoginService([{"access_token": "should-not-be-used"}])
    _patch_common(monkeypatch, hour=1, minute=0)  # well before 05:35
    notifications = FakeNotifications()
    reconciler = FakeJournalReconciler()

    await _run_briefly(_settings(tmp_path), notifications, reconciler, ticks=4)

    assert _shared_login_service.call_count == 0

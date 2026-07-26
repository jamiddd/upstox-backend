from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import anyio

from app.core.config import Settings
from app.core.exceptions import UpstoxApiError
from app.services import max_loss_watcher as watcher
from app.services.instrument_rules_service import InstrumentRulesService

_IST = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN_NOW = datetime(2026, 7, 21, 10, 0, tzinfo=_IST)  # a Tuesday, mid-session
_MARKET_CLOSED_NOW = datetime(2026, 7, 21, 20, 0, tzinfo=_IST)


class _FakeTokenStore:
    def __init__(self, *, has_token: bool = True) -> None:
        self._has_token = has_token

    def has_token(self) -> bool:
        return self._has_token

    def load_access_token(self) -> str:
        return "upstox-token"


class _FakeSettingsStore:
    def __init__(self, amount: float) -> None:
        self.amount = amount
        self.load_calls = 0
        self.cleared = False

    def load(self) -> float:
        self.load_calls += 1
        return self.amount

    def clear(self) -> None:
        self.cleared = True
        self.amount = 0.0


class _RaceSettingsStore(_FakeSettingsStore):
    """First .load() (the pre-lock check) sees the real threshold; every call after that (the
    re-check inside the lock) sees it as already disarmed -- simulates the client's own check
    winning the race in between."""

    def load(self) -> float:
        self.load_calls += 1
        return self.amount if self.load_calls == 1 else 0.0


class _FakeUpstox:
    def __init__(self, pnl_values: list[float]) -> None:
        self._pnl_values = pnl_values

    async def get_positions(self, access_token: str) -> dict[str, Any]:
        return {
            "data": [
                {"instrument_token": f"NSE_FO|{i}", "quantity": 75, "pnl": pnl}
                for i, pnl in enumerate(self._pnl_values)
            ]
        }


class _FailingUpstox:
    async def get_positions(self, access_token: str) -> dict[str, Any]:
        raise UpstoxApiError("Upstox is down")


class _FakeSmartOrderService:
    def __init__(self, *, raises: Optional[Exception] = None) -> None:
        self.raises = raises
        self.calls = 0

    async def exit_all_positions(
        self, access_token: str, *, instrument_rules_service: InstrumentRulesService,
    ) -> dict[str, Any]:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return {"status": "success", "positions_found": 1, "results": []}


class _FakeNotificationService:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append(kwargs)
        return {"id": len(self.recorded), **kwargs}


def _run(
    *,
    now: datetime = _MARKET_OPEN_NOW,
    token_store: Any = None,
    settings_store: Any,
    upstox: Any,
    smart_order_service: Any = None,
    notification_service: Any = None,
) -> tuple[Any, Any]:
    notification_service = notification_service or _FakeNotificationService()
    smart_order_service = smart_order_service or _FakeSmartOrderService()

    async def go() -> None:
        lock = asyncio.Lock()
        await watcher._check_once(
            now=now,
            token_store=token_store or _FakeTokenStore(),
            settings_store=settings_store,
            upstox=upstox,
            smart_order_service=smart_order_service,
            instrument_rules_service=InstrumentRulesService(_settings_stub()),
            notification_service=notification_service,
            exit_all_lock=lock,
        )

    anyio.run(go)
    return smart_order_service, notification_service


def _settings_stub() -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/token.enc"),
    )


def test_positions_pnl_sums_numeric_pnl_fields() -> None:
    payload = {
        "data": [
            {"pnl": 100.5},
            {"pnl": -250.25},
            {"pnl": "not-a-number"},
            {"other": "field"},
        ]
    }
    assert watcher._positions_pnl(payload) == 100.5 - 250.25


def test_check_once_noop_when_market_closed() -> None:
    settings_store = _FakeSettingsStore(1000.0)
    smart_order_service, notifications = _run(
        now=_MARKET_CLOSED_NOW,
        settings_store=settings_store,
        upstox=_FakeUpstox([-2000.0]),
    )
    assert smart_order_service.calls == 0
    assert notifications.recorded == []


def test_check_once_noop_when_threshold_disabled() -> None:
    settings_store = _FakeSettingsStore(0.0)
    smart_order_service, notifications = _run(
        settings_store=settings_store,
        upstox=_FakeUpstox([-5000.0]),
    )
    assert smart_order_service.calls == 0
    assert notifications.recorded == []


def test_check_once_noop_when_no_token() -> None:
    settings_store = _FakeSettingsStore(1000.0)
    smart_order_service, notifications = _run(
        token_store=_FakeTokenStore(has_token=False),
        settings_store=settings_store,
        upstox=_FakeUpstox([-5000.0]),
    )
    assert smart_order_service.calls == 0
    assert notifications.recorded == []


def test_check_once_noop_when_pnl_has_not_breached_threshold() -> None:
    settings_store = _FakeSettingsStore(1000.0)
    smart_order_service, notifications = _run(
        settings_store=settings_store,
        upstox=_FakeUpstox([-500.0]),  # loss, but not past the 1000 threshold
    )
    assert smart_order_service.calls == 0
    assert notifications.recorded == []
    assert not settings_store.cleared


def test_check_once_flattens_and_disarms_on_breach() -> None:
    settings_store = _FakeSettingsStore(1000.0)
    smart_order_service, notifications = _run(
        settings_store=settings_store,
        upstox=_FakeUpstox([-600.0, -500.0]),  # total -1100, breaches -1000
    )
    assert smart_order_service.calls == 1
    assert settings_store.cleared
    assert len(notifications.recorded) == 1
    assert notifications.recorded[0]["category"] == "risk"
    assert notifications.recorded[0]["severity"] == "critical"
    assert notifications.recorded[0]["title"] == "Max-loss auto square-off triggered"


def test_check_once_notifies_without_disarming_when_flatten_fails() -> None:
    settings_store = _FakeSettingsStore(1000.0)
    failing_service = _FakeSmartOrderService(raises=UpstoxApiError("margin call rejected"))
    _, notifications = _run(
        settings_store=settings_store,
        upstox=_FakeUpstox([-2000.0]),
        smart_order_service=failing_service,
    )
    assert failing_service.calls == 1
    assert not settings_store.cleared  # stays armed -- next tick retries
    assert len(notifications.recorded) == 1
    assert notifications.recorded[0]["title"] == "Max-loss auto square-off failed"


def test_check_once_skips_if_disarmed_by_a_concurrent_trigger_after_acquiring_lock() -> None:
    """Covers the race this whole lock exists for: the client's own check wins and flattens
    first, disarming the threshold in the moment between this watcher's own pre-lock breach
    check and it actually acquiring the lock."""
    settings_store = _RaceSettingsStore(1000.0)
    smart_order_service, notifications = _run(
        settings_store=settings_store,
        upstox=_FakeUpstox([-2000.0]),
    )
    assert smart_order_service.calls == 0
    assert notifications.recorded == []


def test_check_once_noop_when_positions_fetch_fails() -> None:
    settings_store = _FakeSettingsStore(1000.0)
    smart_order_service, notifications = _run(
        settings_store=settings_store,
        upstox=_FailingUpstox(),
    )
    assert smart_order_service.calls == 0
    assert notifications.recorded == []

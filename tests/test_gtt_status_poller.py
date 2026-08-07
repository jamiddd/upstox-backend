from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services import gtt_status_poller
from app.services.gtt_status_poller import run_gtt_status_poller


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="k",
        upstox_api_secret="s",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=tmp_path / "token.enc",
        gtt_database_path=tmp_path / "gtt.sqlite3",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_poller_archives_whatever_upstox_positively_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archived_calls: list[list[dict[str, Any]]] = []

    class FakeStore:
        def __init__(self, settings: Settings) -> None:
            pass

        def archive(self, orders: list[dict[str, Any]]) -> None:
            archived_calls.append(orders)

    class FakeTokenStore:
        def __init__(self, settings: Settings) -> None:
            pass

        def has_token(self) -> bool:
            return True

        def load_access_token(self) -> str:
            return "token"

    class FakeSmartOrderService:
        def __init__(self, upstox: object) -> None:
            pass

        async def get_all_gtt_orders(self, access_token: str) -> list[dict[str, Any]]:
            return [{"gtt_order_id": "GTT-1", "instrument_token": "NSE_FO|111", "status": "ACTIVE"}]

    monkeypatch.setattr(gtt_status_poller, "GttHistoryStore", FakeStore)
    monkeypatch.setattr(gtt_status_poller, "EncryptedTokenStore", FakeTokenStore)
    monkeypatch.setattr(gtt_status_poller, "SmartOrderService", FakeSmartOrderService)
    monkeypatch.setattr(gtt_status_poller, "UpstoxService", lambda settings: object())
    monkeypatch.setattr(gtt_status_poller, "_POLL_SECONDS", 0.0)

    task = asyncio.create_task(run_gtt_status_poller(_settings(tmp_path)))
    for _ in range(6):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(archived_calls) >= 1
    assert archived_calls[0][0]["gtt_order_id"] == "GTT-1"


@pytest.mark.anyio
async def test_poller_skips_the_cycle_without_touching_the_store_when_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No point fetching Upstox's list at all (let alone archiving anything) if this backend
    isn't even logged in yet -- has_token() gates the whole cycle, same posture every other
    scheduler in this codebase already uses (see account_snapshot_scheduler.py)."""
    archived_calls: list[Any] = []

    class FakeStore:
        def __init__(self, settings: Settings) -> None:
            pass

        def archive(self, orders: list[dict[str, Any]]) -> None:
            archived_calls.append(orders)

    class FakeTokenStore:
        def __init__(self, settings: Settings) -> None:
            pass

        def has_token(self) -> bool:
            return False

        def load_access_token(self) -> str:
            raise AssertionError("should never be called when has_token() is False")

    monkeypatch.setattr(gtt_status_poller, "GttHistoryStore", FakeStore)
    monkeypatch.setattr(gtt_status_poller, "EncryptedTokenStore", FakeTokenStore)
    monkeypatch.setattr(gtt_status_poller, "UpstoxService", lambda settings: object())
    monkeypatch.setattr(gtt_status_poller, "_POLL_SECONDS", 0.0)

    task = asyncio.create_task(run_gtt_status_poller(_settings(tmp_path)))
    for _ in range(6):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert archived_calls == []

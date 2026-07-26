from __future__ import annotations

from typing import Any

import anyio
import pytest

from app.core.exceptions import UpstoxApiError
from app.services.position_pnl_tracker import PositionPnlTracker


class _FakeTokenStore:
    def __init__(self, *, has_token: bool = True) -> None:
        self._has_token = has_token

    def has_token(self) -> bool:
        return self._has_token

    def load_access_token(self) -> str:
        return "upstox-token"


class _FakeUpstox:
    def __init__(self, positions: list[dict[str, Any]]) -> None:
        self._positions = positions
        self.calls = 0

    async def get_positions(self, access_token: str) -> dict[str, Any]:
        self.calls += 1
        return {"data": self._positions}


class _FailingUpstox:
    async def get_positions(self, access_token: str) -> dict[str, Any]:
        raise UpstoxApiError("Upstox is down")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_total_pnl_is_zero_before_any_refresh() -> None:
    tracker = PositionPnlTracker(_FakeUpstox([]), _FakeTokenStore())

    assert tracker.total_pnl() == 0.0
    assert tracker.instrument_keys() == set()


@pytest.mark.anyio
async def test_refresh_populates_snapshot_and_total_pnl() -> None:
    upstox = _FakeUpstox(
        [
            {"instrument_token": "NSE_FO|111", "quantity": 75, "last_price": 125.0, "pnl": 375.0},
            {"instrument_token": "NSE_FO|222", "quantity": -150, "last_price": 85.0, "pnl": 750.0},
        ]
    )
    tracker = PositionPnlTracker(upstox, _FakeTokenStore())

    await tracker.refresh()

    assert tracker.total_pnl() == 375.0 + 750.0
    assert tracker.instrument_keys() == {"NSE_FO|111", "NSE_FO|222"}


@pytest.mark.anyio
async def test_apply_tick_adjusts_pnl_by_price_delta_times_quantity() -> None:
    """Same formula Android's own MainViewModel.handleTick uses for its live P&L display:
    live_pnl = snapshot_pnl + (tick_ltp - snapshot_last_price) * quantity."""
    upstox = _FakeUpstox(
        [{"instrument_token": "NSE_FO|111", "quantity": 75, "last_price": 125.0, "pnl": 375.0}]
    )
    tracker = PositionPnlTracker(upstox, _FakeTokenStore())
    await tracker.refresh()

    tracker.apply_tick("NSE_FO|111", 130.0)  # price moved up 5.0

    assert tracker.total_pnl() == 375.0 + 5.0 * 75


@pytest.mark.anyio
async def test_apply_tick_for_an_instrument_with_no_open_position_is_ignored() -> None:
    tracker = PositionPnlTracker(_FakeUpstox([]), _FakeTokenStore())

    tracker.apply_tick("NSE_FO|999", 100.0)

    assert tracker.total_pnl() == 0.0


@pytest.mark.anyio
async def test_apply_tick_with_none_ltp_is_ignored() -> None:
    upstox = _FakeUpstox(
        [{"instrument_token": "NSE_FO|111", "quantity": 75, "last_price": 125.0, "pnl": 375.0}]
    )
    tracker = PositionPnlTracker(upstox, _FakeTokenStore())
    await tracker.refresh()

    tracker.apply_tick("NSE_FO|111", None)

    assert tracker.total_pnl() == 375.0


@pytest.mark.anyio
async def test_refresh_drops_live_tick_for_a_position_that_closed_since() -> None:
    upstox = _FakeUpstox(
        [{"instrument_token": "NSE_FO|111", "quantity": 75, "last_price": 125.0, "pnl": 375.0}]
    )
    tracker = PositionPnlTracker(upstox, _FakeTokenStore())
    await tracker.refresh()
    tracker.apply_tick("NSE_FO|111", 130.0)
    assert tracker.total_pnl() != 375.0  # live tick applied

    upstox._positions = []  # position fully closed
    await tracker.refresh()

    assert tracker.total_pnl() == 0.0
    assert tracker.instrument_keys() == set()


@pytest.mark.anyio
async def test_refresh_is_a_noop_when_no_token() -> None:
    upstox = _FakeUpstox(
        [{"instrument_token": "NSE_FO|111", "quantity": 75, "last_price": 125.0, "pnl": 375.0}]
    )
    tracker = PositionPnlTracker(upstox, _FakeTokenStore(has_token=False))

    await tracker.refresh()

    assert upstox.calls == 0
    assert tracker.total_pnl() == 0.0


@pytest.mark.anyio
async def test_refresh_keeps_previous_snapshot_when_upstox_call_fails() -> None:
    upstox = _FakeUpstox(
        [{"instrument_token": "NSE_FO|111", "quantity": 75, "last_price": 125.0, "pnl": 375.0}]
    )
    tracker = PositionPnlTracker(upstox, _FakeTokenStore())
    await tracker.refresh()

    tracker._upstox = _FailingUpstox()
    await tracker.refresh()

    assert tracker.total_pnl() == 375.0  # unchanged, previous snapshot retained

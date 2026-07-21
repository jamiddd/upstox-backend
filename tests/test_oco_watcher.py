from __future__ import annotations

from typing import Any, Optional

import anyio
import pytest

from app.core.exceptions import UpstoxApiError, UpstoxAuthRequiredError
from app.services import oco_watcher as watcher
from app.services.pending_oco_pairs_store import OcoPair


class _FakeTokenStore:
    def __init__(self, *, token: Optional[str] = "upstox-token") -> None:
        self._token = token

    def has_token(self) -> bool:
        return self._token is not None

    def load_access_token(self) -> str:
        if self._token is None:
            raise UpstoxAuthRequiredError("Upstox login is required")
        return self._token


class _FakePendingStore:
    def __init__(self, pairs: list[OcoPair]) -> None:
        self._pairs = pairs
        self.removed: list[OcoPair] = []

    def load(self) -> list[OcoPair]:
        return self._pairs

    def remove(self, pairs_to_remove: list[OcoPair]) -> None:
        self.removed.extend(pairs_to_remove)
        self._pairs = [pair for pair in self._pairs if pair not in pairs_to_remove]


class _FakeUpstox:
    def __init__(self, order_statuses: dict[str, str]) -> None:
        self._order_statuses = order_statuses
        self.cancelled_order_ids: list[str] = []
        self.fail_cancel_for: set[str] = set()
        self.order_book_raises: Optional[Exception] = None

    async def get_order_book(self, access_token: str) -> dict[str, Any]:
        if self.order_book_raises is not None:
            raise self.order_book_raises
        return {
            "status": "success",
            "data": [
                {"order_id": order_id, "status": status}
                for order_id, status in self._order_statuses.items()
            ],
        }

    async def cancel_order(self, access_token: str, order_id: str) -> dict[str, Any]:
        if order_id in self.fail_cancel_for:
            raise UpstoxApiError("Cancel failed", status_code=400)
        self.cancelled_order_ids.append(order_id)
        return {"status": "success", "data": {"order_id": order_id}}


@pytest.fixture(autouse=True)
def _market_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults every test to "market is open" -- individual tests override via monkeypatch."""
    monkeypatch.setattr(watcher, "is_market_open", lambda: True)


def _run(**kwargs: Any) -> None:
    anyio.run(lambda: watcher._reconcile_pending_pairs(**kwargs))


def test_skips_everything_when_market_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "is_market_open", lambda: False)
    pair = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    pending_store = _FakePendingStore([pair])
    upstox = _FakeUpstox({"T-1": "complete", "S-1": "open"})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.cancelled_order_ids == []
    assert pending_store.removed == []


def test_skips_everything_when_no_pairs_are_pending() -> None:
    pending_store = _FakePendingStore([])
    upstox = _FakeUpstox({})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.cancelled_order_ids == []


def test_skips_everything_when_no_token_is_stored() -> None:
    pair = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    pending_store = _FakePendingStore([pair])
    upstox = _FakeUpstox({"T-1": "complete", "S-1": "open"})

    _run(token_store=_FakeTokenStore(token=None), pending_store=pending_store, upstox=upstox)

    assert upstox.cancelled_order_ids == []
    assert pending_store.removed == []


def test_leaves_pair_alone_while_both_legs_are_still_live() -> None:
    pair = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    pending_store = _FakePendingStore([pair])
    upstox = _FakeUpstox({"T-1": "open", "S-1": "trigger pending"})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.cancelled_order_ids == []
    assert pending_store.removed == []


def test_cancels_stoploss_leg_when_target_fills() -> None:
    pair = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    pending_store = _FakePendingStore([pair])
    upstox = _FakeUpstox({"T-1": "complete", "S-1": "open"})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.cancelled_order_ids == ["S-1"]
    assert pending_store.removed == [pair]


def test_cancels_target_leg_when_stoploss_fills() -> None:
    pair = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    pending_store = _FakePendingStore([pair])
    upstox = _FakeUpstox({"T-1": "open", "S-1": "complete"})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.cancelled_order_ids == ["T-1"]
    assert pending_store.removed == [pair]


def test_drops_pair_without_cancelling_when_sibling_already_terminal() -> None:
    """If the sibling leg is already cancelled/rejected (e.g. the user cancelled it manually via
    the app's own Order History screen), there's nothing left to cancel -- just drop the pair."""
    pair = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    pending_store = _FakePendingStore([pair])
    upstox = _FakeUpstox({"T-1": "complete", "S-1": "cancelled"})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.cancelled_order_ids == []
    assert pending_store.removed == [pair]


def test_drops_pair_even_when_the_cancel_call_itself_fails() -> None:
    """Best-effort -- a failed cancel (e.g. a race with a manual cancel) still resolves the pair,
    since there's nothing further the watcher can do about it."""
    pair = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    pending_store = _FakePendingStore([pair])
    upstox = _FakeUpstox({"T-1": "complete", "S-1": "open"})
    upstox.fail_cancel_for.add("S-1")

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert pending_store.removed == [pair]


def test_one_pair_failing_does_not_stop_the_others() -> None:
    resolved_pair = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    unresolved_pair = OcoPair(target_order_id="T-2", stoploss_order_id="S-2", instrument_key="NSE_FO|222")
    pending_store = _FakePendingStore([resolved_pair, unresolved_pair])
    upstox = _FakeUpstox({"T-1": "complete", "S-1": "open", "T-2": "open", "S-2": "open"})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.cancelled_order_ids == ["S-1"]
    assert pending_store.removed == [resolved_pair]


def test_stops_reconciling_when_the_order_book_fetch_fails() -> None:
    pair = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    pending_store = _FakePendingStore([pair])
    upstox = _FakeUpstox({"T-1": "complete", "S-1": "open"})
    upstox.order_book_raises = UpstoxApiError("Order book unavailable", status_code=502)

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.cancelled_order_ids == []
    assert pending_store.removed == []

from __future__ import annotations

from typing import Any, Optional

import anyio
import pytest

from app.core.exceptions import UpstoxApiError, UpstoxAuthRequiredError
from app.services import oco_watcher as watcher
from app.services.pending_oco_pairs_store import PendingExit


def _pending_exit(
    stoploss_order_id: str = "S-1",
    instrument_key: str = "NSE_FO|111",
    exit_transaction_type: str = "SELL",
    quantity: int = 75,
    product: str = "I",
    target_trigger_price: float = 140.0,
) -> PendingExit:
    return PendingExit(
        stoploss_order_id=stoploss_order_id,
        instrument_key=instrument_key,
        exit_transaction_type=exit_transaction_type,
        quantity=quantity,
        product=product,
        target_trigger_price=target_trigger_price,
    )


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
    def __init__(self, pending_exits: list[PendingExit]) -> None:
        self._pending_exits = pending_exits
        self.removed: list[PendingExit] = []

    def load(self) -> list[PendingExit]:
        return self._pending_exits

    def remove(self, pending_exits_to_remove: list[PendingExit]) -> None:
        self.removed.extend(pending_exits_to_remove)
        self._pending_exits = [
            pending_exit for pending_exit in self._pending_exits if pending_exit not in pending_exits_to_remove
        ]


class _FakeUpstox:
    def __init__(self, order_statuses: dict[str, str], last_prices: dict[str, float]) -> None:
        self._order_statuses = order_statuses
        self._last_prices = last_prices
        self.cancelled_order_ids: list[str] = []
        self.placed_orders: list[dict[str, Any]] = []
        self.fail_cancel_for: set[str] = set()
        self.fail_place_order: bool = False
        self.order_book_raises: Optional[Exception] = None
        self.quotes_raise: Optional[Exception] = None
        self.requested_instrument_keys: list[str] = []

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

    async def get_quotes(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        self.requested_instrument_keys.append(instrument_key)
        if self.quotes_raise is not None:
            raise self.quotes_raise
        return {
            "status": "success",
            "data": {
                key: {"instrument_token": key, "last_price": price}
                for key, price in self._last_prices.items()
            },
        }

    async def cancel_order(self, access_token: str, order_id: str) -> dict[str, Any]:
        if order_id in self.fail_cancel_for:
            raise UpstoxApiError("Cancel failed", status_code=400)
        self.cancelled_order_ids.append(order_id)
        return {"status": "success", "data": {"order_id": order_id}}

    async def place_order(self, access_token: str, **kwargs: Any) -> dict[str, Any]:
        if self.fail_place_order:
            raise UpstoxApiError("Order rejected", status_code=400)
        self.placed_orders.append(kwargs)
        return {"status": "success", "data": {"order_id": "MKT-1"}}


@pytest.fixture(autouse=True)
def _market_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults every test to "market is open" -- individual tests override via monkeypatch."""
    monkeypatch.setattr(watcher, "is_market_open", lambda: True)


def _run(**kwargs: Any) -> None:
    anyio.run(lambda: watcher._reconcile_pending_exits(**kwargs))


def test_skips_everything_when_market_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "is_market_open", lambda: False)
    pending_store = _FakePendingStore([_pending_exit()])
    upstox = _FakeUpstox({"S-1": "open"}, {"NSE_FO|111": 150.0})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.placed_orders == []
    assert pending_store.removed == []


def test_skips_everything_when_no_pending_exits() -> None:
    pending_store = _FakePendingStore([])
    upstox = _FakeUpstox({}, {})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.placed_orders == []


def test_skips_everything_when_no_token_is_stored() -> None:
    pending_store = _FakePendingStore([_pending_exit()])
    upstox = _FakeUpstox({"S-1": "open"}, {"NSE_FO|111": 150.0})

    _run(token_store=_FakeTokenStore(token=None), pending_store=pending_store, upstox=upstox)

    assert upstox.placed_orders == []
    assert pending_store.removed == []


def test_drops_pending_exit_when_stoploss_has_filled() -> None:
    """The stop was hit -- position already closed by it, nothing left to watch."""
    pending_exit = _pending_exit(stoploss_order_id="S-1")
    pending_store = _FakePendingStore([pending_exit])
    upstox = _FakeUpstox({"S-1": "complete"}, {"NSE_FO|111": 150.0})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert pending_store.removed == [pending_exit]
    assert upstox.placed_orders == []
    # No quote lookup needed -- resolved via the order book alone.
    assert upstox.requested_instrument_keys == []


def test_drops_pending_exit_when_stoploss_is_cancelled_or_rejected() -> None:
    """The protective leg is gone (e.g. manually cancelled) -- drop rather than keep watching a
    target with no stop backing it."""
    pending_exit = _pending_exit(stoploss_order_id="S-1")
    pending_store = _FakePendingStore([pending_exit])
    upstox = _FakeUpstox({"S-1": "cancelled"}, {"NSE_FO|111": 150.0})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert pending_store.removed == [pending_exit]
    assert upstox.placed_orders == []


def test_leaves_pending_exit_alone_while_stoploss_is_live_and_target_not_reached() -> None:
    pending_exit = _pending_exit(stoploss_order_id="S-1", exit_transaction_type="SELL", target_trigger_price=140.0)
    pending_store = _FakePendingStore([pending_exit])
    upstox = _FakeUpstox({"S-1": "open"}, {"NSE_FO|111": 130.0})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.placed_orders == []
    assert pending_store.removed == []


def test_fires_target_when_price_crosses_for_a_long_exit() -> None:
    """exit_transaction_type SELL closes a long -- target reached once price rises to/through it."""
    pending_exit = _pending_exit(
        stoploss_order_id="S-1",
        instrument_key="NSE_FO|111",
        exit_transaction_type="SELL",
        quantity=75,
        product="I",
        target_trigger_price=140.0,
    )
    pending_store = _FakePendingStore([pending_exit])
    upstox = _FakeUpstox({"S-1": "open"}, {"NSE_FO|111": 140.5})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert len(upstox.placed_orders) == 1
    placed = upstox.placed_orders[0]
    assert placed["instrument_key"] == "NSE_FO|111"
    assert placed["transaction_type"] == "SELL"
    assert placed["quantity"] == 75
    assert placed["product"] == "I"
    assert placed["order_type"] == "MARKET"
    assert upstox.cancelled_order_ids == ["S-1"]
    assert pending_store.removed == [pending_exit]


def test_fires_target_when_price_crosses_for_a_short_exit() -> None:
    """exit_transaction_type BUY closes a short -- target reached once price falls to/through it."""
    pending_exit = _pending_exit(
        stoploss_order_id="S-1",
        instrument_key="NSE_FO|111",
        exit_transaction_type="BUY",
        target_trigger_price=100.0,
    )
    pending_store = _FakePendingStore([pending_exit])
    upstox = _FakeUpstox({"S-1": "open"}, {"NSE_FO|111": 99.0})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert len(upstox.placed_orders) == 1
    assert upstox.placed_orders[0]["transaction_type"] == "BUY"
    assert pending_store.removed == [pending_exit]


def test_cancels_stoploss_before_removing_pending_exit_and_placing_the_market_order() -> None:
    """Ordering: cancel the resting stoploss first (otherwise the market order below competes
    with it for the same exit-side exposure and gets rejected for margin), *then* remove from the
    store right before place_order -- so a crash between remove() and place_order() leaves the
    worst case as "a missed target fire" (next tick sees the now-cancelled stoploss and drops the
    pending exit as resolved), never a double-placed market order. Simulated here by making
    place_order raise and asserting the cancel had already happened but the store removal had too."""
    pending_exit = _pending_exit(stoploss_order_id="S-1", target_trigger_price=140.0)
    pending_store = _FakePendingStore([pending_exit])
    upstox = _FakeUpstox({"S-1": "open"}, {"NSE_FO|111": 145.0})
    upstox.fail_place_order = True

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.cancelled_order_ids == ["S-1"]
    assert pending_store.removed == [pending_exit]
    assert upstox.placed_orders == []


def test_target_fire_is_skipped_this_tick_when_cancelling_the_stoploss_fails() -> None:
    """If the stoploss can't be cancelled (including a race where it just filled/went terminal),
    the market order must not fire on top of it -- skip this tick, leave the pending exit tracked
    so the next tick re-evaluates it (and correctly drops it once the order book reflects
    whatever the stoploss's new status actually is) instead of firing a now-wrong order."""
    pending_exit = _pending_exit(stoploss_order_id="S-1", target_trigger_price=140.0)
    pending_store = _FakePendingStore([pending_exit])
    upstox = _FakeUpstox({"S-1": "open"}, {"NSE_FO|111": 145.0})
    upstox.fail_cancel_for.add("S-1")

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.placed_orders == []
    assert pending_store.removed == []


def test_one_pending_exit_failing_does_not_stop_the_others() -> None:
    resolved = _pending_exit(stoploss_order_id="S-1", instrument_key="NSE_FO|111", target_trigger_price=140.0)
    unresolved = _pending_exit(stoploss_order_id="S-2", instrument_key="NSE_FO|222", target_trigger_price=200.0)
    pending_store = _FakePendingStore([resolved, unresolved])
    upstox = _FakeUpstox(
        {"S-1": "open", "S-2": "open"},
        {"NSE_FO|111": 145.0, "NSE_FO|222": 150.0},
    )

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert len(upstox.placed_orders) == 1
    assert upstox.placed_orders[0]["instrument_key"] == "NSE_FO|111"
    assert pending_store.removed == [resolved]


def test_stops_reconciling_when_the_order_book_fetch_fails() -> None:
    pending_exit = _pending_exit(stoploss_order_id="S-1")
    pending_store = _FakePendingStore([pending_exit])
    upstox = _FakeUpstox({"S-1": "open"}, {"NSE_FO|111": 150.0})
    upstox.order_book_raises = UpstoxApiError("Order book unavailable", status_code=502)

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.placed_orders == []
    assert pending_store.removed == []


def test_stops_reconciling_when_the_quotes_fetch_fails_but_still_drops_terminal_ones() -> None:
    """A quotes-fetch failure only blocks the still-live pending exits (which need a price to
    check) -- one already resolved via the order book alone is still dropped."""
    resolved = _pending_exit(stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    still_live = _pending_exit(stoploss_order_id="S-2", instrument_key="NSE_FO|222")
    pending_store = _FakePendingStore([resolved, still_live])
    upstox = _FakeUpstox({"S-1": "complete", "S-2": "open"}, {})
    upstox.quotes_raise = UpstoxApiError("Quotes unavailable", status_code=502)

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.placed_orders == []
    assert pending_store.removed == [resolved]


def test_batches_quote_lookup_across_pending_exits_for_the_same_instrument() -> None:
    a = _pending_exit(stoploss_order_id="S-1", instrument_key="NSE_FO|111", target_trigger_price=999.0)
    b = _pending_exit(stoploss_order_id="S-2", instrument_key="NSE_FO|111", target_trigger_price=999.0)
    pending_store = _FakePendingStore([a, b])
    upstox = _FakeUpstox({"S-1": "open", "S-2": "open"}, {"NSE_FO|111": 100.0})

    _run(token_store=_FakeTokenStore(), pending_store=pending_store, upstox=upstox)

    assert upstox.requested_instrument_keys == ["NSE_FO|111"]

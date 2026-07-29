from __future__ import annotations

import pytest

from app.services.feed_subscription_manager import D30SubscriptionLimitError, FeedSubscriptionManager


class _FakeTrackedStore:
    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def load(self) -> list[str]:
        return self._keys


class _FakeMarketFeedClient:
    def __init__(self) -> None:
        self.full_calls: list[list[str]] = []
        self.d30_calls: list[list[str]] = []
        self.ltpc_subscribe_calls: list[list[str]] = []
        self.unsubscribe_calls: list[list[str]] = []
        self._full: list[str] = []
        self._d30: list[str] = []
        self._ltpc: list[str] = []

    async def replace_subscriptions(
        self, *, full_d30: list[str], full: list[str], ltpc: list[str],
    ) -> None:
        if full_d30 != self._d30:
            self.d30_calls.append(full_d30)
            self._d30 = full_d30
        if full != self._full:
            self.full_calls.append(full)
            self._full = full
        if ltpc != self._ltpc:
            if self._ltpc:
                self.unsubscribe_calls.append(self._ltpc)
            if ltpc:
                self.ltpc_subscribe_calls.append(ltpc)
            self._ltpc = ltpc

    async def replace_full_subscription(self, instrument_keys: list[str]) -> None:
        self.full_calls.append(instrument_keys)

    async def subscribe_ltpc(self, instrument_keys: list[str]) -> None:
        self.ltpc_subscribe_calls.append(instrument_keys)

    async def unsubscribe(self, instrument_keys: list[str]) -> None:
        self.unsubscribe_calls.append(instrument_keys)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_tracked_instruments_are_always_in_full_subscription() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(
        market_feed_client=client, tracked_store=_FakeTrackedStore(["NSE_INDEX|Nifty 50"]),
    )

    await manager.refresh_tracked_instruments()

    assert client.full_calls[-1] == ["NSE_INDEX|Nifty 50"]


@pytest.mark.anyio
async def test_client_full_subscription_unions_with_tracked_set() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(
        market_feed_client=client, tracked_store=_FakeTrackedStore(["NSE_INDEX|Nifty 50"]),
    )

    await manager.set_client_subscription("session-1", full=["NSE_FO|111"], ltpc=[])

    assert client.full_calls[-1] == sorted(["NSE_INDEX|Nifty 50", "NSE_FO|111"])


@pytest.mark.anyio
async def test_multiple_client_sessions_union_together() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(market_feed_client=client, tracked_store=_FakeTrackedStore([]))

    await manager.set_client_subscription("session-1", full=["A"], ltpc=[])
    await manager.set_client_subscription("session-2", full=["B"], ltpc=[])

    assert client.full_calls[-1] == sorted(["A", "B"])


@pytest.mark.anyio
async def test_removing_a_client_drops_its_contribution() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(market_feed_client=client, tracked_store=_FakeTrackedStore([]))
    await manager.set_client_subscription("session-1", full=["A"], ltpc=[])
    await manager.set_client_subscription("session-2", full=["B"], ltpc=[])

    await manager.remove_client("session-1")

    assert client.full_calls[-1] == ["B"]


@pytest.mark.anyio
async def test_full_mode_wins_over_ltpc_for_the_same_key() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(market_feed_client=client, tracked_store=_FakeTrackedStore([]))

    await manager.set_client_subscription("session-1", full=["A"], ltpc=["A", "B"])

    assert client.full_calls[-1] == ["A"]
    assert client.ltpc_subscribe_calls[-1] == ["B"]


@pytest.mark.anyio
async def test_ltpc_set_change_unsubscribes_previous_before_subscribing_new() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(market_feed_client=client, tracked_store=_FakeTrackedStore([]))
    await manager.set_client_subscription("session-1", full=[], ltpc=["A", "B"])
    client.unsubscribe_calls.clear()
    client.ltpc_subscribe_calls.clear()

    await manager.set_client_subscription("session-1", full=[], ltpc=["B", "C"])

    assert client.unsubscribe_calls == [["A", "B"]]
    assert client.ltpc_subscribe_calls == [["B", "C"]]


@pytest.mark.anyio
async def test_open_position_instruments_are_always_in_ltpc_subscription() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(market_feed_client=client, tracked_store=_FakeTrackedStore([]))

    await manager.set_open_position_instruments({"NSE_FO|111"})

    assert client.ltpc_subscribe_calls[-1] == ["NSE_FO|111"]


@pytest.mark.anyio
async def test_open_position_instruments_defer_to_full_mode_for_the_same_key() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(
        market_feed_client=client, tracked_store=_FakeTrackedStore(["NSE_FO|111"]),
    )

    await manager.set_open_position_instruments({"NSE_FO|111"})

    assert client.full_calls[-1] == ["NSE_FO|111"]
    assert client.ltpc_subscribe_calls == []


@pytest.mark.anyio
async def test_open_position_instruments_union_with_client_ltpc_wants() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(market_feed_client=client, tracked_store=_FakeTrackedStore([]))
    await manager.set_client_subscription("session-1", full=[], ltpc=["A"])
    client.ltpc_subscribe_calls.clear()

    await manager.set_open_position_instruments({"B"})

    assert client.ltpc_subscribe_calls[-1] == sorted(["A", "B"])


@pytest.mark.anyio
async def test_closed_position_drops_out_of_ltpc_subscription() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(market_feed_client=client, tracked_store=_FakeTrackedStore([]))
    await manager.set_open_position_instruments({"NSE_FO|111", "NSE_FO|222"})
    client.ltpc_subscribe_calls.clear()

    await manager.set_open_position_instruments({"NSE_FO|111"})

    assert client.ltpc_subscribe_calls[-1] == ["NSE_FO|111"]


@pytest.mark.anyio
async def test_debug_snapshot_breaks_down_by_source() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(
        market_feed_client=client, tracked_store=_FakeTrackedStore(["NSE_INDEX|Nifty 50"]),
    )
    await manager.set_client_subscription("session-1", full=["NSE_FO|111"], ltpc=["NSE_FO|222"])
    await manager.set_open_position_instruments({"NSE_FO|333"})

    snapshot = manager.debug_snapshot()

    assert snapshot["tracked_instruments"] == ["NSE_INDEX|Nifty 50"]
    assert snapshot["position_instruments"] == ["NSE_FO|333"]
    assert snapshot["client_full_by_session"] == {"session-1": ["NSE_FO|111"]}
    assert snapshot["client_ltpc_by_session"] == {"session-1": ["NSE_FO|222"]}


@pytest.mark.anyio
async def test_unchanged_ltpc_set_does_not_resend() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(market_feed_client=client, tracked_store=_FakeTrackedStore([]))
    await manager.set_client_subscription("session-1", full=[], ltpc=["A"])
    client.unsubscribe_calls.clear()
    client.ltpc_subscribe_calls.clear()

    await manager.set_client_subscription("session-2", full=["X"], ltpc=[])  # unrelated change

    assert client.unsubscribe_calls == []
    assert client.ltpc_subscribe_calls == []


@pytest.mark.anyio
async def test_d30_is_reserved_for_client_keys_and_wins_over_other_modes() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(
        market_feed_client=client, tracked_store=_FakeTrackedStore(["TRACKED", "D30"]),
    )

    await manager.set_client_subscription(
        "session-1", d30=["D30"], full=["D30", "FULL"], ltpc=["D30", "FULL", "LTPC"],
    )

    assert client.d30_calls[-1] == ["D30"]
    assert client.full_calls[-1] == sorted(["FULL", "TRACKED"])
    assert client.ltpc_subscribe_calls[-1] == ["LTPC"]


@pytest.mark.anyio
async def test_d30_limit_rejects_replacement_atomically() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(market_feed_client=client, tracked_store=_FakeTrackedStore([]))
    await manager.set_client_subscription(
        "session-1", d30=[f"K{i}" for i in range(45)], full=[], ltpc=[],
    )

    with pytest.raises(D30SubscriptionLimitError):
        await manager.set_client_subscription("session-2", d30=["EXTRA"], full=[], ltpc=[])

    assert manager.debug_snapshot()["client_d30_by_session"] == {
        "session-1": [f"K{i}" for i in sorted(range(45), key=lambda value: f"K{value}")],
    }

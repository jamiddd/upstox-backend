from __future__ import annotations

import pytest

from app.services.feed_subscription_manager import FeedSubscriptionManager


class _FakeTrackedStore:
    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def load(self) -> list[str]:
        return self._keys


class _FakeMarketFeedClient:
    def __init__(self) -> None:
        self.full_calls: list[list[str]] = []
        self.ltpc_subscribe_calls: list[list[str]] = []
        self.unsubscribe_calls: list[list[str]] = []

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
async def test_unchanged_ltpc_set_does_not_resend() -> None:
    client = _FakeMarketFeedClient()
    manager = FeedSubscriptionManager(market_feed_client=client, tracked_store=_FakeTrackedStore([]))
    await manager.set_client_subscription("session-1", full=[], ltpc=["A"])
    client.unsubscribe_calls.clear()
    client.ltpc_subscribe_calls.clear()

    await manager.set_client_subscription("session-2", full=["X"], ltpc=[])  # unrelated change

    assert client.unsubscribe_calls == []
    assert client.ltpc_subscribe_calls == []

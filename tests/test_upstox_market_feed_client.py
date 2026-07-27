from __future__ import annotations

import pytest

from app.generated import MarketDataFeed_pb2 as pb
from app.services import upstox_market_feed_client as market_feed_client_module
from app.services.upstox_market_feed_client import (
    FeedCandle,
    FeedTick,
    MarketDepthLevel,
    UpstoxMarketFeedClient,
    decode_feed_response,
)


class _FakeClock:
    """Injectable stand-in for time.monotonic(), same reasoning as
    market_hours.is_market_open's own injectable `now` -- lets staleness tests control elapsed
    time exactly instead of racing the real clock."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FakeUnderlyingClient:
    """Stands in for the inner UpstoxWebSocketClient so subscription-diff tests don't need a real
    connection -- only records what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.connected = True

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)


def _client() -> tuple[UpstoxMarketFeedClient, _FakeUnderlyingClient]:
    ticks: list[FeedTick] = []
    market_client = UpstoxMarketFeedClient(
        upstox=None,  # type: ignore[arg-type]
        token_store=None,  # type: ignore[arg-type]
        on_tick=ticks.append,
    )
    fake = _FakeUnderlyingClient()
    market_client._client = fake  # type: ignore[assignment]
    return market_client, fake


def test_decodes_ltpc_only_tick() -> None:
    ltpc = pb.LTPC(ltp=125.5)
    feed = pb.Feed(ltpc=ltpc)
    response = pb.FeedResponse()
    response.feeds["NSE_FO|111"].CopyFrom(feed)

    ticks = decode_feed_response(response.SerializeToString())

    assert ticks == [FeedTick(instrument_key="NSE_FO|111", ltp=125.5, last_trade_time_millis=0)]


def test_decodes_full_mode_market_contract_with_bid_ask_and_candle() -> None:
    ltpc = pb.LTPC(ltp=125.0)
    ohlc = pb.OHLC(interval="I1", ts=1_721_873_700_000, open=124.0, high=126.0, low=123.5, close=125.0, vol=900)
    market_ohlc = pb.MarketOHLC(ohlc=[ohlc])
    quotes = [
        pb.Quote(bidQ=150, bidP=124.5, askQ=225, askP=125.5),
        pb.Quote(bidQ=300, bidP=124.0, askQ=375, askP=126.0),
    ]
    market_level = pb.MarketLevel(bidAskQuote=quotes)
    market_full_feed = pb.MarketFullFeed(
        ltpc=ltpc,
        marketLevel=market_level,
        marketOHLC=market_ohlc,
        tbq=93_950,
        tsq=116_950,
    )
    full_feed = pb.FullFeed(marketFF=market_full_feed)
    feed = pb.Feed(fullFeed=full_feed)
    response = pb.FeedResponse()
    response.feeds["NSE_FO|222"].CopyFrom(feed)

    ticks = decode_feed_response(response.SerializeToString())

    assert len(ticks) == 1
    tick = ticks[0]
    assert tick.instrument_key == "NSE_FO|222"
    assert tick.ltp == 125.0
    assert tick.bid_price == 124.5
    assert tick.ask_price == 125.5
    assert tick.market_depth == (
        MarketDepthLevel(150, 124.5, 225, 125.5),
        MarketDepthLevel(300, 124.0, 375, 126.0),
    )
    assert tick.total_bid_quantity == 93_950
    assert tick.total_ask_quantity == 116_950
    assert tick.one_minute_candle == FeedCandle(
        timestamp_millis=1_721_873_700_000, open=124.0, high=126.0, low=123.5, close=125.0, volume=900,
    )


def test_decodes_index_full_feed_with_no_bid_ask() -> None:
    ltpc = pb.LTPC(ltp=25_050.0)
    index_full_feed = pb.IndexFullFeed(ltpc=ltpc)
    full_feed = pb.FullFeed(indexFF=index_full_feed)
    feed = pb.Feed(fullFeed=full_feed)
    response = pb.FeedResponse()
    response.feeds["NSE_INDEX|Nifty 50"].CopyFrom(feed)

    ticks = decode_feed_response(response.SerializeToString())

    assert len(ticks) == 1
    assert ticks[0].ltp == 25_050.0
    assert ticks[0].bid_price is None
    assert ticks[0].ask_price is None


def test_malformed_bytes_return_no_ticks() -> None:
    assert decode_feed_response(b"not a protobuf message \xff\xfe") == []


@pytest.mark.anyio
async def test_replace_full_subscription_diffs_and_preserves_common_instruments() -> None:
    client, fake = _client()

    await client.replace_full_subscription(["A", "B"])
    assert fake.sent == [
        {
            "guid": fake.sent[0]["guid"],
            "method": "sub",
            "data": {"mode": "full_d30", "instrumentKeys": ["A", "B"]},
        },
    ]

    fake.sent.clear()
    await client.replace_full_subscription(["B", "C"])

    # "A" removed, "B" retained without re-sending, "C" added -- exactly one unsub and one sub.
    methods = [(m["method"], m["data"]["mode"], m["data"]["instrumentKeys"]) for m in fake.sent]
    assert ("unsub", "ltpc", ["A"]) in methods
    assert ("sub", "full_d30", ["C"]) in methods


@pytest.mark.anyio
async def test_replace_full_subscription_restores_overlapping_ltpc_subscription() -> None:
    client, fake = _client()
    client._desired_ltpc = ["A"]  # already LTPC-subscribed for e.g. an open position

    await client.replace_full_subscription(["A"])
    fake.sent.clear()
    await client.replace_full_subscription([])  # drop "A" from full mode entirely

    methods = [(m["method"], m["data"]["mode"], m["data"]["instrumentKeys"]) for m in fake.sent]
    assert ("unsub", "ltpc", ["A"]) in methods
    # "A" is still wanted in LTPC mode, so it must be resubscribed after the unsub side-effect.
    assert ("sub", "ltpc", ["A"]) in methods


@pytest.mark.anyio
async def test_replace_full_subscription_is_noop_when_unchanged() -> None:
    client, fake = _client()
    await client.replace_full_subscription(["A"])
    fake.sent.clear()

    await client.replace_full_subscription(["A"])

    assert fake.sent == []


@pytest.mark.anyio
async def test_unsubscribe_removes_from_both_desired_sets() -> None:
    client, fake = _client()
    await client.replace_full_subscription(["A", "B"])
    await client.subscribe_ltpc(["A"])
    fake.sent.clear()

    await client.unsubscribe(["A"])

    assert client._desired_full == ["B"]
    assert client._desired_ltpc == []


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_resend_stale_subscriptions_does_not_nudge_freshly_subscribed_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(market_feed_client_module.time, "monotonic", clock)
    client, fake = _client()

    await client.replace_full_subscription(["A"])
    fake.sent.clear()

    clock.advance(10.0)  # under the 30s threshold used below
    nudged = await client.resend_stale_subscriptions(stale_after_seconds=30.0)

    assert nudged == []
    assert fake.sent == []


@pytest.mark.anyio
async def test_resend_stale_subscriptions_does_not_nudge_recently_ticked_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(market_feed_client_module.time, "monotonic", clock)
    client, fake = _client()
    await client.replace_full_subscription(["A"])

    clock.advance(100.0)  # well past the threshold, but a tick just arrived
    client._on_message(_full_feed_message("A", ltp=100.0))
    fake.sent.clear()

    nudged = await client.resend_stale_subscriptions(stale_after_seconds=30.0)

    assert nudged == []
    assert fake.sent == []


@pytest.mark.anyio
async def test_resend_stale_subscriptions_nudges_stale_full_mode_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(market_feed_client_module.time, "monotonic", clock)
    client, fake = _client()
    await client.replace_full_subscription(["A"])
    fake.sent.clear()

    clock.advance(30.0)
    nudged = await client.resend_stale_subscriptions(stale_after_seconds=30.0)

    assert nudged == ["A"]
    assert fake.sent == [
        {
            "guid": fake.sent[0]["guid"],
            "method": "sub",
            "data": {"mode": "full_d30", "instrumentKeys": ["A"]},
        },
    ]


@pytest.mark.anyio
async def test_resend_stale_subscriptions_nudges_stale_ltpc_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(market_feed_client_module.time, "monotonic", clock)
    client, fake = _client()
    await client.subscribe_ltpc(["A"])
    fake.sent.clear()

    clock.advance(30.0)
    nudged = await client.resend_stale_subscriptions(stale_after_seconds=30.0)

    assert nudged == ["A"]
    assert fake.sent == [
        {
            "guid": fake.sent[0]["guid"],
            "method": "sub",
            "data": {"mode": "ltpc", "instrumentKeys": ["A"]},
        },
    ]


@pytest.mark.anyio
async def test_resend_stale_subscriptions_nudges_once_via_full_when_desired_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(market_feed_client_module.time, "monotonic", clock)
    client, fake = _client()
    await client.replace_full_subscription(["A"])
    await client.subscribe_ltpc(["A"])
    fake.sent.clear()

    clock.advance(30.0)
    nudged = await client.resend_stale_subscriptions(stale_after_seconds=30.0)

    assert nudged == ["A"]
    assert len(fake.sent) == 1
    assert fake.sent[0]["data"]["mode"] == "full_d30"


@pytest.mark.anyio
async def test_resend_stale_subscriptions_does_not_immediately_renudge_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(market_feed_client_module.time, "monotonic", clock)
    client, fake = _client()
    await client.replace_full_subscription(["A"])

    clock.advance(30.0)
    first_nudge = await client.resend_stale_subscriptions(stale_after_seconds=30.0)
    assert first_nudge == ["A"]

    clock.advance(1.0)  # well under another full 30s window since the nudge
    second_check = await client.resend_stale_subscriptions(stale_after_seconds=30.0)

    assert second_check == []


@pytest.mark.anyio
async def test_resend_stale_subscriptions_is_noop_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(market_feed_client_module.time, "monotonic", clock)
    client, fake = _client()
    await client.replace_full_subscription(["A"])
    fake.sent.clear()
    fake.connected = False
    clock.advance(100.0)

    nudged = await client.resend_stale_subscriptions(stale_after_seconds=30.0)

    assert nudged == []
    assert fake.sent == []


def _full_feed_message(instrument_key: str, *, ltp: float) -> bytes:
    ltpc = pb.LTPC(ltp=ltp)
    market_full_feed = pb.MarketFullFeed(ltpc=ltpc)
    full_feed = pb.FullFeed(marketFF=market_full_feed)
    feed = pb.Feed(fullFeed=full_feed)
    response = pb.FeedResponse()
    response.feeds[instrument_key].CopyFrom(feed)
    return response.SerializeToString()

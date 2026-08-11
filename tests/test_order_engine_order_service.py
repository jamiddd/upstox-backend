import pytest

from app.services.order_engine_order_service import (
    OrderEngineOrderService,
    derive_order_tag,
)


class FakeUpstox:
    """Small fake recording place_order calls and returning a scripted order book.
    cancel_order/modify_order mutate order_book_data in place, same as a real broker would, so a
    service method's *second* find_existing_order call (the post-mutation confirm) sees the
    updated state, not a frozen snapshot."""

    def __init__(self, order_book_data=None) -> None:
        self.place_order_calls: list[dict] = []
        self.cancel_order_calls: list[str] = []
        self.modify_order_calls: list[dict] = []
        self.order_book_data = order_book_data if order_book_data is not None else []

    async def get_order_book(self, access_token):
        return {"status": "success", "data": self.order_book_data}

    async def place_order(self, access_token, **kwargs):
        self.place_order_calls.append(kwargs)
        return {"status": "success", "data": {"order_id": "broker-order-1"}}

    async def cancel_order(self, access_token, order_id):
        self.cancel_order_calls.append(order_id)
        for order in self.order_book_data:
            if order.get("order_id") == order_id:
                order["status"] = "cancelled"
        return {"status": "success", "data": {"order_id": order_id}}

    async def modify_order(self, access_token, order):
        self.modify_order_calls.append(order)
        for existing in self.order_book_data:
            if existing.get("order_id") == order.get("order_id"):
                existing["quantity"] = order.get("quantity")
        return {"status": "success", "data": {"order_id": order.get("order_id")}}


def test_derive_order_tag_is_deterministic_and_alphanumeric():
    tag_one = derive_order_tag("rule-1")
    tag_two = derive_order_tag("rule-1")
    tag_three = derive_order_tag("rule-2")

    assert tag_one == tag_two
    assert tag_one != tag_three
    assert len(tag_one) <= 20
    assert tag_one.isalnum()


@pytest.mark.anyio
async def test_place_order_places_a_new_order_when_none_exists_yet():
    fake = FakeUpstox()
    service = OrderEngineOrderService(fake)

    result = await service.place_order(
        "token",
        idempotency_key="rule-1",
        instrument_key="NSE_FO|123",
        transaction_type="SELL",
        quantity=50,
        product="I",
        order_type="MARKET",
    )

    assert result.already_existed is False
    assert result.order["data"]["order_id"] == "broker-order-1"
    assert len(fake.place_order_calls) == 1
    assert fake.place_order_calls[0]["tag"] == derive_order_tag("rule-1")


@pytest.mark.anyio
async def test_place_order_returns_the_existing_order_without_placing_a_duplicate():
    tag = derive_order_tag("rule-1")
    fake = FakeUpstox(order_book_data=[{"order_id": "already-placed", "status": "complete", "tag": tag}])
    service = OrderEngineOrderService(fake)

    result = await service.place_order(
        "token",
        idempotency_key="rule-1",
        instrument_key="NSE_FO|123",
        transaction_type="SELL",
        quantity=50,
        product="I",
        order_type="MARKET",
    )

    assert result.already_existed is True
    assert result.order["order_id"] == "already-placed"
    assert fake.place_order_calls == [] # never called Upstox's place endpoint at all


@pytest.mark.anyio
async def test_find_existing_order_matches_by_derived_tag_only():
    tag = derive_order_tag("rule-1")
    other_tag = derive_order_tag("rule-2")
    fake = FakeUpstox(
        order_book_data=[
            {"order_id": "other-order", "status": "complete", "tag": other_tag},
            {"order_id": "the-one", "status": "open", "tag": tag},
        ],
    )
    service = OrderEngineOrderService(fake)

    found = await service.find_existing_order("token", "rule-1")

    assert found is not None
    assert found["order_id"] == "the-one"


@pytest.mark.anyio
async def test_find_existing_order_returns_none_when_nothing_matches():
    fake = FakeUpstox(order_book_data=[{"order_id": "unrelated", "status": "complete", "tag": "different-tag"}])
    service = OrderEngineOrderService(fake)

    found = await service.find_existing_order("token", "rule-1")

    assert found is None


@pytest.mark.anyio
async def test_find_existing_order_handles_a_malformed_order_book_gracefully():
    fake = FakeUpstox(order_book_data=None)
    fake.order_book_data = None

    async def get_order_book(access_token):
        return {"status": "error"}  # no "data" key at all

    fake.get_order_book = get_order_book
    service = OrderEngineOrderService(fake)

    found = await service.find_existing_order("token", "rule-1")

    assert found is None


@pytest.mark.anyio
async def test_cancel_order_cancels_and_returns_the_confirmed_post_cancel_state():
    tag = derive_order_tag("rule-1")
    fake = FakeUpstox(order_book_data=[{"order_id": "broker-order-1", "status": "open", "tag": tag}])
    service = OrderEngineOrderService(fake)

    result = await service.cancel_order("token", "rule-1")

    assert fake.cancel_order_calls == ["broker-order-1"]
    assert result is not None
    assert result["status"] == "cancelled"  # the confirmed re-fetch, not a stale ack


@pytest.mark.anyio
async def test_cancel_order_returns_none_when_nothing_matches_and_never_calls_cancel():
    fake = FakeUpstox(order_book_data=[])
    service = OrderEngineOrderService(fake)

    result = await service.cancel_order("token", "rule-1")

    assert result is None
    assert fake.cancel_order_calls == []


@pytest.mark.anyio
async def test_modify_order_quantity_carries_over_every_other_field_unchanged():
    tag = derive_order_tag("rule-1")
    fake = FakeUpstox(
        order_book_data=[
            {
                "order_id": "broker-order-1",
                "status": "open",
                "tag": tag,
                "quantity": 50,
                "price": 105.5,
                "order_type": "LIMIT",
                "trigger_price": 0,
                "validity": "DAY",
            },
        ],
    )
    service = OrderEngineOrderService(fake)

    result = await service.modify_order_quantity("token", "rule-1", 75)

    assert len(fake.modify_order_calls) == 1
    sent = fake.modify_order_calls[0]
    assert sent["order_id"] == "broker-order-1"
    assert sent["quantity"] == 75
    assert sent["price"] == 105.5  # carried over, not caller-supplied
    assert sent["order_type"] == "LIMIT"
    assert sent["validity"] == "DAY"
    assert result is not None
    assert result["quantity"] == 75  # the confirmed re-fetch


@pytest.mark.anyio
async def test_modify_order_quantity_returns_none_when_nothing_matches_and_never_calls_modify():
    fake = FakeUpstox(order_book_data=[])
    service = OrderEngineOrderService(fake)

    result = await service.modify_order_quantity("token", "rule-1", 75)

    assert result is None
    assert fake.modify_order_calls == []

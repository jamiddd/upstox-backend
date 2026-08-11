import pytest

from app.services.order_engine_order_service import (
    OrderEngineOrderService,
    UnintendedShortGuardError,
    derive_order_tag,
)


class FakeUpstox:
    """Small fake recording place_order calls and returning a scripted order book.
    cancel_order/modify_order mutate order_book_data in place, same as a real broker would, so a
    service method's *second* find_existing_order call (the post-mutation confirm) sees the
    updated state, not a frozen snapshot."""

    def __init__(self, order_book_data=None, positions_data=None) -> None:
        self.place_order_calls: list[dict] = []
        self.cancel_order_calls: list[str] = []
        self.modify_order_calls: list[dict] = []
        self.order_book_data = order_book_data if order_book_data is not None else []
        self.positions_data = positions_data if positions_data is not None else []

    async def get_positions(self, access_token):
        return {"status": "success", "data": self.positions_data}

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


# -- guard_against_unintended_short -- "assume the user doesn't want to open a short, it's a
# mistake" (direct product direction). See OrderEngineOrderService.place_order's own doc comment.


@pytest.mark.anyio
async def test_resolve_held_long_quantity_returns_the_matching_positions_quantity():
    fake = FakeUpstox(positions_data=[{"instrument_token": "NSE_FO|1", "quantity": 50}])
    service = OrderEngineOrderService(fake)

    held = await service.resolve_held_long_quantity("token", "NSE_FO|1")

    assert held == 50.0


@pytest.mark.anyio
async def test_resolve_held_long_quantity_floors_a_short_position_at_zero():
    fake = FakeUpstox(positions_data=[{"instrument_token": "NSE_FO|1", "quantity": -20}])
    service = OrderEngineOrderService(fake)

    held = await service.resolve_held_long_quantity("token", "NSE_FO|1")

    assert held == 0.0


@pytest.mark.anyio
async def test_resolve_held_long_quantity_is_zero_when_instrument_not_in_positions_at_all():
    fake = FakeUpstox(positions_data=[{"instrument_token": "NSE_FO|OTHER", "quantity": 50}])
    service = OrderEngineOrderService(fake)

    held = await service.resolve_held_long_quantity("token", "NSE_FO|1")

    assert held == 0.0


@pytest.mark.anyio
async def test_place_order_guard_allows_a_sell_within_the_held_quantity():
    fake = FakeUpstox(positions_data=[{"instrument_token": "NSE_FO|1", "quantity": 50}])
    service = OrderEngineOrderService(fake)

    result = await service.place_order(
        "token", idempotency_key="entry-1", instrument_key="NSE_FO|1",
        transaction_type="SELL", quantity=50, product="I", order_type="MARKET",
        guard_against_unintended_short=True,
    )

    assert result.already_existed is False
    assert len(fake.place_order_calls) == 1


@pytest.mark.anyio
async def test_place_order_guard_rejects_a_sell_exceeding_the_held_quantity():
    fake = FakeUpstox(positions_data=[{"instrument_token": "NSE_FO|1", "quantity": 30}])
    service = OrderEngineOrderService(fake)

    with pytest.raises(UnintendedShortGuardError):
        await service.place_order(
            "token", idempotency_key="entry-1", instrument_key="NSE_FO|1",
            transaction_type="SELL", quantity=50, product="I", order_type="MARKET",
            guard_against_unintended_short=True,
        )

    assert fake.place_order_calls == []  # never reached Upstox's own placement call at all


@pytest.mark.anyio
async def test_place_order_guard_rejects_a_sell_with_nothing_held_at_all():
    fake = FakeUpstox(positions_data=[])
    service = OrderEngineOrderService(fake)

    with pytest.raises(UnintendedShortGuardError):
        await service.place_order(
            "token", idempotency_key="entry-1", instrument_key="NSE_FO|1",
            transaction_type="SELL", quantity=1, product="I", order_type="MARKET",
            guard_against_unintended_short=True,
        )


@pytest.mark.anyio
async def test_place_order_guard_does_not_apply_to_buy_orders():
    fake = FakeUpstox(positions_data=[])  # nothing held, irrelevant for a BUY
    service = OrderEngineOrderService(fake)

    result = await service.place_order(
        "token", idempotency_key="entry-1", instrument_key="NSE_FO|1",
        transaction_type="BUY", quantity=50, product="I", order_type="MARKET",
        guard_against_unintended_short=True,
    )

    assert result.already_existed is False
    assert len(fake.place_order_calls) == 1


@pytest.mark.anyio
async def test_place_order_guard_is_off_by_default_and_never_checks_positions():
    fake = FakeUpstox(positions_data=[])  # would reject if the guard ran
    service = OrderEngineOrderService(fake)

    result = await service.place_order(
        "token", idempotency_key="entry-1", instrument_key="NSE_FO|1",
        transaction_type="SELL", quantity=50, product="I", order_type="MARKET",
    )

    assert result.already_existed is False
    assert len(fake.place_order_calls) == 1


# -- resolve_closeable_quantity -- flatten_open_lots' broker-truth cap, bidirectional (long-close
# via SELL, short-cover via BUY), distinct from resolve_held_long_quantity (SELL-only guard use).


@pytest.mark.anyio
async def test_resolve_closeable_quantity_for_a_long_lot_reads_the_held_long_magnitude():
    fake = FakeUpstox(positions_data=[{"instrument_token": "NSE_FO|1", "quantity": 40}])
    service = OrderEngineOrderService(fake)

    closeable = await service.resolve_closeable_quantity("token", "NSE_FO|1", "BUY")

    assert closeable == 40.0


@pytest.mark.anyio
async def test_resolve_closeable_quantity_for_a_short_lot_reads_the_held_short_magnitude():
    fake = FakeUpstox(positions_data=[{"instrument_token": "NSE_FO|1", "quantity": -25}])
    service = OrderEngineOrderService(fake)

    closeable = await service.resolve_closeable_quantity("token", "NSE_FO|1", "SELL")

    assert closeable == 25.0


@pytest.mark.anyio
async def test_resolve_closeable_quantity_is_zero_for_a_long_lot_when_the_broker_shows_a_short():
    fake = FakeUpstox(positions_data=[{"instrument_token": "NSE_FO|1", "quantity": -25}])
    service = OrderEngineOrderService(fake)

    closeable = await service.resolve_closeable_quantity("token", "NSE_FO|1", "BUY")

    assert closeable == 0.0


@pytest.mark.anyio
async def test_resolve_closeable_quantity_is_zero_when_nothing_is_held_at_all():
    fake = FakeUpstox(positions_data=[])
    service = OrderEngineOrderService(fake)

    assert await service.resolve_closeable_quantity("token", "NSE_FO|1", "BUY") == 0.0
    assert await service.resolve_closeable_quantity("token", "NSE_FO|1", "SELL") == 0.0

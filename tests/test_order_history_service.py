from __future__ import annotations

from app.services.order_history_service import _shape_today_order


def test_shape_today_order_carries_the_real_tag_through() -> None:
    # Regression, 2026-08-13: found live via the order engine -- this used to silently drop
    # Upstox's own "tag" field, leaving the client with no way to match a broker order back to a
    # TriggerRule from order-history data (only from a live order_update push).
    order = {
        "order_id": "260813000123456",
        "instrument_token": "NSE_FO|1",
        "trading_symbol": "TEST",
        "transaction_type": "SELL",
        "status": "complete",
        "quantity": 50,
        "tag": "abc123de",
    }

    shaped = _shape_today_order(order)

    assert shaped["tag"] == "abc123de"


def test_shape_today_order_tag_is_blank_when_absent() -> None:
    # _string_value's own contract: missing/non-string -> "", never None -- same as every other
    # field this function shapes, not a special case for tag.
    order = {
        "order_id": "260813000123456",
        "instrument_token": "NSE_FO|1",
        "trading_symbol": "TEST",
        "transaction_type": "SELL",
        "status": "complete",
        "quantity": 50,
    }

    shaped = _shape_today_order(order)

    assert shaped["tag"] == ""

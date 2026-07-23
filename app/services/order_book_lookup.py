from __future__ import annotations

from typing import Any, Optional

# Terminal statuses that mean an order is done moving -- shared by oco_watcher (decides whether a
# leg has filled/died) and SmartOrderService._cancel_resting_stoploss_orders (skips a stoploss
# that's already gone terminal but oco_watcher hasn't reconciled it yet this tick).
TERMINAL_ORDER_STATUSES = {"complete", "cancelled", "rejected"}


def index_orders_by_id(order_book_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Upstox's raw `GET /order/retrieve-all` payload, keyed by each order's own order_id."""
    data = order_book_payload.get("data")
    orders = data if isinstance(data, list) else []
    indexed: dict[str, dict[str, Any]] = {}
    for order in orders:
        if not isinstance(order, dict):
            continue
        order_id = order.get("order_id")
        if isinstance(order_id, str):
            indexed[order_id] = order
    return indexed


def order_status(order: Optional[dict[str, Any]]) -> Optional[str]:
    if order is None:
        return None
    status = order.get("status")
    return status.lower() if isinstance(status, str) else None

from __future__ import annotations

from typing import Any, Optional

from app.services.upstox_service import UpstoxService

"""Shared broker order-book lookup, factored out of `ExitReconciliationChecker._find_order` when
Part 5 (`docs/ORDER_HISTORY_V2_DESIGN.md`) needed the identical fetch-and-filter-by-order_id logic
for its own re-confirm-before-trusting-the-WS-push step -- one implementation, not two copies of
the same broker call."""


async def find_order_by_id(
    upstox: UpstoxService, access_token: str, order_id: str
) -> Optional[dict[str, Any]]:
    """Fetches today's full order book and returns the entry whose `order_id` matches, or `None`
    if it isn't there. Upstox has no "get order by id" endpoint -- the order book is the only
    place this lookup can happen, same reasoning `OrderEngineOrderService.find_existing_order`
    already documents for its own tag-based variant of this same fetch-and-filter pattern."""
    book = await upstox.get_order_book(access_token)
    orders = book.get("data") if isinstance(book, dict) else None
    if not isinstance(orders, list):
        return None
    for order in orders:
        if isinstance(order, dict) and order.get("order_id") == order_id:
            return order
    return None

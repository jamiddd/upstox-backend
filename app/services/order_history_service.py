from __future__ import annotations

from datetime import date
from typing import Any, Optional

from app.services.upstox_service import UpstoxService


class OrderHistoryService:
    """Build order-history screen payloads."""

    def __init__(self, upstox_service: UpstoxService) -> None:
        self.upstox = upstox_service

    async def today_orders(
        self,
        access_token: str,
        *,
        page_number: int,
        page_size: int,
    ) -> dict[str, Any]:
        """Return today's orders, sorted newest first and categorized by status."""
        payload = await self.upstox.get_order_book(access_token)
        orders = sorted(
            [_shape_today_order(order) for order in _list_data(payload)],
            key=lambda order: order["timestamp"] or "",
            reverse=True,
        )
        page_items, page = _paginate(orders, page_number=page_number, page_size=page_size)
        return {
            "scope": "today",
            "source": "order_book",
            "orders": page_items,
            "categories": _categorize(page_items),
            "page": page,
        }

    async def historical_orders(
        self,
        access_token: str,
        *,
        page_number: int,
        page_size: int,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        segment: str = "FO",
    ) -> dict[str, Any]:
        """Return historical executed trades as the available past-order view."""
        start = start_date or _default_history_start_date()
        end = end_date or date.today().isoformat()
        payload = await self.upstox.get_historical_trades(
            access_token,
            segment=segment,
            start_date=start,
            end_date=end,
            page_number=page_number,
            page_size=page_size,
        )
        orders = sorted(
            [_shape_historical_trade(trade) for trade in _list_data(payload)],
            key=lambda order: order["trade_date"] or "",
            reverse=True,
        )
        return {
            "scope": "all",
            "source": "historical_trades",
            "availability_note": (
                "Upstox exposes past executed trades, not all past rejected/cancelled orders."
            ),
            "orders": orders,
            "categories": _categorize(orders),
            "page": _page(payload, page_number=page_number, page_size=page_size),
            "filters": {
                "segment": segment,
                "start_date": start,
                "end_date": end,
            },
        }


def _shape_today_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _string_value(order, "order_id"),
        "instrument_key": _string_value(order, "instrument_token"),
        "trading_symbol": _string_value(order, "trading_symbol", "tradingsymbol"),
        "transaction_type": _string_value(order, "transaction_type"),
        "order_type": _string_value(order, "order_type"),
        "product": _string_value(order, "product"),
        "status": _string_value(order, "status"),
        "quantity": _number_value(order, "quantity"),
        "filled_quantity": _number_value(order, "filled_quantity"),
        "pending_quantity": _number_value(order, "pending_quantity"),
        "price": _number_value(order, "price"),
        "average_price": _number_value(order, "average_price"),
        "trigger_price": _number_value(order, "trigger_price"),
        "timestamp": _string_value(order, "order_timestamp"),
        "exchange_timestamp": _string_value(order, "exchange_timestamp"),
        "status_message": _string_value(order, "status_message"),
    }


def _shape_historical_trade(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _string_value(trade, "trade_id"),
        "instrument_key": _string_value(trade, "instrument_token"),
        "trading_symbol": _string_value(trade, "symbol", "scrip_name"),
        "transaction_type": _string_value(trade, "transaction_type"),
        "status": "complete",
        "quantity": _number_value(trade, "quantity"),
        "price": _number_value(trade, "price"),
        "amount": _number_value(trade, "amount"),
        "exchange": _string_value(trade, "exchange"),
        "segment": _string_value(trade, "segment"),
        "option_type": _string_value(trade, "option_type"),
        "strike_price": _string_value(trade, "strike_price"),
        "expiry": _string_value(trade, "expiry"),
        "trade_date": _string_value(trade, "trade_date"),
    }


def _categorize(orders: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    categories = {
        "open": [],
        "complete": [],
        "cancelled": [],
        "rejected": [],
        "other": [],
    }
    for order in orders:
        status = str(order.get("status", "")).lower()
        if status in {"open", "trigger pending", "validation pending", "put order req received"}:
            categories["open"].append(order)
        elif status == "complete":
            categories["complete"].append(order)
        elif status == "cancelled":
            categories["cancelled"].append(order)
        elif status == "rejected":
            categories["rejected"].append(order)
        else:
            categories["other"].append(order)
    return categories


def _paginate(
    items: list[dict[str, Any]],
    *,
    page_number: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    start = (page_number - 1) * page_size
    end = start + page_size
    total_records = len(items)
    total_pages = (total_records + page_size - 1) // page_size if total_records else 0
    return (
        items[start:end],
        {
            "page_number": page_number,
            "page_size": page_size,
            "total_records": total_records,
            "total_pages": total_pages,
        },
    )


def _page(payload: dict[str, Any], *, page_number: int, page_size: int) -> dict[str, int]:
    meta_data = payload.get("meta_data")
    page = meta_data.get("page") if isinstance(meta_data, dict) else None
    if not isinstance(page, dict):
        return {
            "page_number": page_number,
            "page_size": page_size,
            "total_records": len(_list_data(payload)),
            "total_pages": page_number,
        }
    return {
        "page_number": _int_value(page, "page_number", default=page_number),
        "page_size": _int_value(page, "page_size", default=page_size),
        "total_records": _int_value(page, "total_records", default=0),
        "total_pages": _int_value(page, "total_pages", default=page_number),
    }


def _default_history_start_date() -> str:
    today = date.today()
    current_fy_start_year = today.year if today.month >= 4 else today.year - 1
    return date(current_fy_start_year - 2, 4, 1).isoformat()


def _list_data(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _string_value(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str):
            return value
    return ""


def _number_value(payload: dict[str, Any], name: str) -> float:
    value = payload.get(name)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _int_value(payload: dict[str, Any], name: str, *, default: int) -> int:
    value = payload.get(name)
    if isinstance(value, int):
        return value
    return default

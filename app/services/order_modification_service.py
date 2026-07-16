from __future__ import annotations

from typing import Any

from app.core.exceptions import UpstoxApiError
from app.services.upstox_service import UpstoxService


class OrderModificationService:
    """Modify an arbitrary number of orders and preserve per-order results."""

    def __init__(self, upstox_service: UpstoxService) -> None:
        self.upstox = upstox_service

    async def modify_orders(
        self,
        access_token: str,
        orders: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Attempt every modification, even when an earlier order fails."""
        results: list[dict[str, Any]] = []
        success_count = 0

        for order in orders:
            order_id = order["order_id"]
            try:
                response = await self.upstox.modify_order(access_token, order)
            except UpstoxApiError as exc:
                error: dict[str, Any] = {
                    "message": exc.message,
                    "upstox_code": exc.upstox_code,
                }
                if exc.details is not None:
                    error["details"] = exc.details
                results.append(
                    {
                        "order_id": order_id,
                        "status": "error",
                        "error": error,
                    }
                )
                continue

            success_count += 1
            results.append(
                {
                    "order_id": order_id,
                    "status": "success",
                    "upstox_response": response,
                }
            )

        failed_count = len(orders) - success_count
        if failed_count == 0:
            overall_status = "success"
        elif success_count == 0:
            overall_status = "error"
        else:
            overall_status = "partial_success"

        return {
            "status": overall_status,
            "summary": {
                "total": len(orders),
                "success": success_count,
                "failed": failed_count,
            },
            "orders": results,
        }

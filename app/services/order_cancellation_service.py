from __future__ import annotations

from typing import Any

from app.core.exceptions import UpstoxApiError
from app.services.upstox_service import UpstoxService


class OrderCancellationService:
    """Cancel an arbitrary number of open orders and preserve per-order results -- same
    best-effort shape as OrderModificationService.modify_orders (one order failing doesn't stop
    the rest, and the caller gets each order's own outcome back).
    """

    def __init__(self, upstox_service: UpstoxService) -> None:
        self.upstox = upstox_service

    async def cancel_orders(
        self,
        access_token: str,
        order_ids: list[str],
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        success_count = 0

        for order_id in order_ids:
            try:
                response = await self.upstox.cancel_order(access_token, order_id)
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

        failed_count = len(order_ids) - success_count
        if failed_count == 0:
            overall_status = "success"
        elif success_count == 0:
            overall_status = "error"
        else:
            overall_status = "partial_success"

        return {
            "status": overall_status,
            "summary": {
                "total": len(order_ids),
                "success": success_count,
                "failed": failed_count,
            },
            "orders": results,
        }

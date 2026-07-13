from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions import UpstoxApiError
from app.services.upstox_service import UpstoxService


class SmartOrderService:
    """Translate app smart-order requests into Upstox GTT orders."""

    def __init__(self, upstox_service: UpstoxService) -> None:
        self.upstox = upstox_service

    async def place_bracket_order(
        self,
        access_token: str,
        *,
        instrument_key: str,
        transaction_type: str,
        quantity: int,
        product: str,
        entry_trigger_type: str,
        entry_trigger_price: float,
        target_trigger_price: float,
        stoploss_trigger_price: float,
        slice_quantity: int,
        trailing_gap: Optional[float] = None,
        market_protection: Optional[int] = None,
    ) -> dict[str, Any]:
        """Place an entry + target + stoploss GTT order."""
        slices = _split_quantity(quantity, slice_quantity)
        placed_slices: list[dict[str, Any]] = []
        for index, slice_qty in enumerate(slices, start=1):
            upstox_order = _build_gtt_order(
                instrument_key=instrument_key,
                transaction_type=transaction_type,
                quantity=slice_qty,
                product=product,
                entry_trigger_type=entry_trigger_type,
                entry_trigger_price=entry_trigger_price,
                target_trigger_price=target_trigger_price,
                stoploss_trigger_price=stoploss_trigger_price,
                trailing_gap=trailing_gap,
                market_protection=market_protection,
            )
            try:
                response = await self.upstox.place_gtt_order(access_token, upstox_order)
            except UpstoxApiError as exc:
                raise UpstoxApiError(
                    "Smart order slicing failed after partial placement",
                    status_code=exc.status_code,
                    upstox_code=exc.upstox_code,
                    details={
                        "placed_slices": placed_slices,
                        "failed_slice": {
                            "slice_number": index,
                            "quantity": slice_qty,
                            "submitted_order": upstox_order,
                        },
                        "upstox_error": exc.details,
                    },
                ) from exc
            placed_slices.append(
                {
                    "slice_number": index,
                    "quantity": slice_qty,
                    "submitted_order": upstox_order,
                    "upstox_response": response,
                }
            )

        return {
            "status": "success",
            "source": "upstox_gtt",
            "total_quantity": quantity,
            "slice_quantity": slice_quantity,
            "slice_count": len(slices),
            "slices": placed_slices,
        }


def _build_gtt_order(
    *,
    instrument_key: str,
    transaction_type: str,
    quantity: int,
    product: str,
    entry_trigger_type: str,
    entry_trigger_price: float,
    target_trigger_price: float,
    stoploss_trigger_price: float,
    trailing_gap: Optional[float],
    market_protection: Optional[int],
) -> dict[str, Any]:
    rules: list[dict[str, Any]] = [
        {
            "strategy": "ENTRY",
            "trigger_type": entry_trigger_type,
            "trigger_price": entry_trigger_price,
        },
        {
            "strategy": "TARGET",
            "trigger_type": "IMMEDIATE",
            "trigger_price": target_trigger_price,
        },
        {
            "strategy": "STOPLOSS",
            "trigger_type": "IMMEDIATE",
            "trigger_price": stoploss_trigger_price,
        },
    ]
    if trailing_gap is not None:
        rules[2]["trailing_gap"] = trailing_gap
    if market_protection is not None:
        for rule in rules:
            rule["market_protection"] = market_protection

    upstox_order = {
        "type": "MULTIPLE",
        "quantity": quantity,
        "product": product,
        "rules": rules,
        "instrument_token": instrument_key,
        "transaction_type": transaction_type,
    }
    return upstox_order


def _split_quantity(quantity: int, slice_quantity: int) -> list[int]:
    safe_slice = max(slice_quantity, 1)
    slices = [safe_slice] * (quantity // safe_slice)
    remainder = quantity % safe_slice
    if remainder:
        slices.append(remainder)
    return slices or [quantity]

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

    async def attach_gtt_exits(
        self,
        access_token: str,
        *,
        instrument_key: str,
        quantity: int,
        product: str,
        exit_transaction_type: str,
        target_trigger_price: float,
        stoploss_trigger_price: float,
    ) -> dict[str, Any]:
        """Attaches a target and a stoploss to an already-open position that has no existing GTT
        bracket, without touching the entry. Places two independent Upstox GTT type=SINGLE orders
        (one rule each) rather than reusing place_bracket_order's type=MULTIPLE shape -- MULTIPLE
        always includes its own ENTRY rule (entry_trigger_type IMMEDIATE fires right away), which
        would submit a second live entry and double the position. [exit_transaction_type] is the
        *exit* side (opposite of how the position was opened -- e.g. "SELL" to exit a long), since
        a SINGLE-type order has no ENTRY leg to infer direction from.

        Best-effort like exit_all_positions: the two legs are independent orders, so one failing
        doesn't stop the other. Returns "success" (both placed), "partial_success" (one placed),
        or "error" (neither placed).
        """
        results: dict[str, dict[str, Any]] = {}
        for key, strategy, trigger_price in (
            ("target", "TARGET", target_trigger_price),
            ("stoploss", "STOPLOSS", stoploss_trigger_price),
        ):
            order = {
                "type": "SINGLE",
                "quantity": quantity,
                "product": product,
                "rules": [
                    {
                        "strategy": strategy,
                        "trigger_type": "IMMEDIATE",
                        "trigger_price": trigger_price,
                    }
                ],
                "instrument_token": instrument_key,
                "transaction_type": exit_transaction_type,
            }
            try:
                response = await self.upstox.place_gtt_order(access_token, order)
                results[key] = {
                    "status": "success",
                    "submitted_order": order,
                    "upstox_response": response,
                }
            except UpstoxApiError as exc:
                results[key] = {
                    "status": "error",
                    "submitted_order": order,
                    "error": str(exc),
                }

        successes = sum(1 for result in results.values() if result["status"] == "success")
        overall_status = "success" if successes == 2 else "partial_success" if successes == 1 else "error"
        return {
            "status": overall_status,
            "target": results["target"],
            "stoploss": results["stoploss"],
        }

    async def get_gtt_orders_for_instrument(
        self, access_token: str, *, instrument_key: str
    ) -> list[dict[str, Any]]:
        """Active GTT orders for one instrument -- lets the app find the bracket order behind an
        open position so its target/stoploss can be edited (see modify_gtt_bracket). "Active"
        excludes terminal statuses (cancelled/rejected/completed); anything else is treated as
        still-live so an unfamiliar status fails open rather than silently hiding a real order.
        """
        payload = await self.upstox.get_gtt_orders(access_token)
        data = payload.get("data")
        orders = data if isinstance(data, list) else []
        return [
            order
            for order in orders
            if isinstance(order, dict)
            and order.get("instrument_token") == instrument_key
            and str(order.get("status", "")).upper() not in _INACTIVE_GTT_STATUSES
        ]

    async def modify_gtt_bracket(
        self,
        access_token: str,
        *,
        gtt_order_id: str,
        quantity: int,
        product: str,
        entry_trigger_type: str,
        entry_trigger_price: float,
        target_trigger_price: float,
        stoploss_trigger_price: float,
        trailing_gap: Optional[float] = None,
    ) -> dict[str, Any]:
        """Re-points an existing GTT bracket's target/stoploss. The entry rule is resent
        unchanged (already triggered, since this only ever runs against an open position) --
        Upstox's GTT modify contract expects the full rule set, not a partial patch.
        """
        rules = _build_gtt_rules(
            entry_trigger_type=entry_trigger_type,
            entry_trigger_price=entry_trigger_price,
            target_trigger_price=target_trigger_price,
            stoploss_trigger_price=stoploss_trigger_price,
            trailing_gap=trailing_gap,
        )
        upstox_order = {
            "gtt_order_id": gtt_order_id,
            "type": "MULTIPLE",
            "quantity": quantity,
            "product": product,
            "rules": rules,
        }
        return await self.upstox.modify_gtt_order(access_token, upstox_order)

    async def exit_all_positions(self, access_token: str) -> dict[str, Any]:
        """Flattens every currently open position -- backs the app's max-loss auto square-off
        (see MainViewModel.watchMaxLoss). Thin wrapper over exit_positions with no filter.
        """
        return await self.exit_positions(access_token)

    async def exit_positions(
        self, access_token: str, *, instrument_keys: Optional[list[str]] = None
    ) -> dict[str, Any]:
        """Flattens open positions (quantity != 0) with an immediate market order in the opposite
        direction. [instrument_keys] is None means every open position (exit_all_positions above);
        otherwise only positions whose instrument_token is in that set are closed -- e.g. "close
        only profitable positions", where the app itself decides which instrument_keys qualify (it
        already has live P&L from the WebSocket feed; the backend doesn't need to re-derive it
        from its own snapshot). Best-effort: one position failing to exit doesn't stop the others
        -- every attempted position's own result (success or error) is returned so the caller/UI
        can show exactly what happened to each one.
        """
        positions_payload = await self.upstox.get_positions(access_token)
        data = positions_payload.get("data")
        open_positions = (
            [item for item in data if isinstance(item, dict) and _position_quantity(item) != 0]
            if isinstance(data, list)
            else []
        )
        if instrument_keys is not None:
            wanted = set(instrument_keys)
            open_positions = [
                item
                for item in open_positions
                if _string_value(item, "instrument_token", "instrument_key") in wanted
            ]

        results: list[dict[str, Any]] = []
        for position in open_positions:
            quantity = _position_quantity(position)
            instrument_key = _string_value(position, "instrument_token", "instrument_key")
            product = _string_value(position, "product") or "I"
            # Signed quantity (positive = net long, negative = net short) -- same convention the
            # rest of this app relies on (see MainViewModel.handleTick's PnL formula) -- so
            # flattening a long is a SELL and flattening a short is a BUY.
            transaction_type = "SELL" if quantity > 0 else "BUY"
            try:
                response = await self.upstox.place_market_order(
                    access_token,
                    instrument_key=instrument_key,
                    transaction_type=transaction_type,
                    quantity=int(abs(quantity)),
                    product=product,
                )
                results.append(
                    {
                        "instrument_key": instrument_key,
                        "transaction_type": transaction_type,
                        "quantity": int(abs(quantity)),
                        "status": "success",
                        "upstox_response": response,
                    }
                )
            except UpstoxApiError as exc:
                results.append(
                    {
                        "instrument_key": instrument_key,
                        "transaction_type": transaction_type,
                        "quantity": int(abs(quantity)),
                        "status": "error",
                        "error": str(exc),
                    }
                )

        return {
            "status": "success",
            "positions_found": len(open_positions),
            "results": results,
        }


# Terminal GTT statuses -- anything else (e.g. a still-pending/triggered rule) is treated as
# active. See SmartOrderService.get_gtt_orders_for_instrument.
_INACTIVE_GTT_STATUSES = {"CANCELLED", "REJECTED", "COMPLETED"}


def _build_gtt_rules(
    *,
    entry_trigger_type: str,
    entry_trigger_price: float,
    target_trigger_price: float,
    stoploss_trigger_price: float,
    trailing_gap: Optional[float],
    market_protection: Optional[int] = None,
) -> list[dict[str, Any]]:
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
    return rules


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
    rules = _build_gtt_rules(
        entry_trigger_type=entry_trigger_type,
        entry_trigger_price=entry_trigger_price,
        target_trigger_price=target_trigger_price,
        stoploss_trigger_price=stoploss_trigger_price,
        trailing_gap=trailing_gap,
        market_protection=market_protection,
    )
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


def _position_quantity(position: dict[str, Any]) -> float:
    value = position.get("quantity")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _string_value(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str):
            return value
    return ""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.core.exceptions import AppConfigError, UpstoxApiError
from app.services.instrument_rules_service import InstrumentRulesService, slice_quantity_for_freeze
from app.services.upstox_service import UpstoxService

_EXIT_MAX_ATTEMPTS = 3
_EXIT_RETRY_DELAY_SECONDS = 1.0

"""Broker-position-truth flatten, extracted from the old `SmartOrderService.exit_positions`
verbatim (freeze-slicing, retry-on-failure, best-effort-per-position semantics all unchanged) so
the order engine's own "exit all" surface can reuse it without depending on `smart_order_service`/
`gtt_history_store` at all -- this operates on Upstox's own `GET /positions` (real held positions,
regardless of which system opened them), not on `order_engine_ledger_store`'s `lots` table, so it
is not itself GTT-specific and needs no successor once the GTT model is retired. The one thing
deliberately dropped versus the original: `_cancel_stray_gtts` -- once no code path ever places a
broker-native GTT again, there is nothing stray left to clean up."""


async def flatten_positions(
    upstox: UpstoxService,
    access_token: str,
    *,
    instrument_rules_service: InstrumentRulesService,
    instrument_keys: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Flattens open positions (quantity != 0) with an immediate market order in the opposite
    direction. [instrument_keys] `None` means every open position; otherwise only positions whose
    instrument_token is in that set are closed. Best-effort: one position failing to exit doesn't
    stop the others -- every attempted position's own result (success or error) is returned so the
    caller/UI can show exactly what happened to each one.

    Each position's own flattening order is sliced by its instrument's freeze quantity (same
    `slice_quantity_for_freeze` machinery order placement uses) so a position sized over freeze
    quantity doesn't silently fail to flatten just because it was submitted as one oversized
    order.
    """
    positions_payload = await upstox.get_positions(access_token)
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

    original_quantities = {
        _string_value(position, "instrument_token", "instrument_key"):
            int(abs(_position_quantity(position)))
        for position in open_positions
    }
    result_by_key: dict[str, dict[str, Any]] = {}
    positions_to_attempt = open_positions

    for attempt in range(1, _EXIT_MAX_ATTEMPTS + 1):
        failed_keys: set[str] = set()
        for position in positions_to_attempt:
            quantity = _position_quantity(position)
            instrument_key = _string_value(position, "instrument_token", "instrument_key")
            product = _string_value(position, "product") or "I"
            # Always use the freshly fetched *remaining* signed quantity on a retry. If an
            # earlier sliced attempt partially succeeded, resubmitting the original quantity
            # could reverse the position instead of flattening it.
            transaction_type = "SELL" if quantity > 0 else "BUY"
            remaining_quantity = int(abs(quantity))
            try:
                rules = await instrument_rules_service.get_rules(instrument_key)
                slice_qty = slice_quantity_for_freeze(remaining_quantity, rules)
            except AppConfigError:
                slice_qty = remaining_quantity
            try:
                upstox_response: Any = None
                for chunk_quantity in _split_quantity(remaining_quantity, slice_qty):
                    upstox_response = await upstox.place_market_order(
                        access_token,
                        instrument_key=instrument_key,
                        transaction_type=transaction_type,
                        quantity=chunk_quantity,
                        product=product,
                    )
                result_by_key[instrument_key] = {
                    "instrument_key": instrument_key,
                    "transaction_type": transaction_type,
                    "quantity": original_quantities[instrument_key],
                    "status": "success",
                    "attempts": attempt,
                    "upstox_response": upstox_response,
                }
            except UpstoxApiError as exc:
                failed_keys.add(instrument_key)
                result_by_key[instrument_key] = {
                    "instrument_key": instrument_key,
                    "transaction_type": transaction_type,
                    "quantity": original_quantities[instrument_key],
                    "status": "error",
                    "attempts": attempt,
                    "error": str(exc),
                }

        if not failed_keys or attempt == _EXIT_MAX_ATTEMPTS:
            break

        await asyncio.sleep(_EXIT_RETRY_DELAY_SECONDS)
        try:
            refreshed_payload = await upstox.get_positions(access_token)
        except UpstoxApiError:
            break
        refreshed_data = refreshed_payload.get("data")
        refreshed_positions = (
            [item for item in refreshed_data if isinstance(item, dict)]
            if isinstance(refreshed_data, list)
            else []
        )
        open_by_key = {
            _string_value(item, "instrument_token", "instrument_key"): item
            for item in refreshed_positions
            if _position_quantity(item) != 0
        }
        positions_to_attempt = [
            open_by_key[key] for key in failed_keys if key in open_by_key
        ]
        # A failed API response can race a fill acknowledgement. If the broker now reports
        # the position flat, treat it as success and never submit a duplicate exit.
        for closed_key in failed_keys - open_by_key.keys():
            previous = result_by_key[closed_key]
            result_by_key[closed_key] = {
                **previous,
                "status": "success",
                "attempts": attempt,
                "error": None,
            }
        if not positions_to_attempt:
            break

    return {
        "status": "success",
        "positions_found": len(open_positions),
        "results": [
            result_by_key[_string_value(position, "instrument_token", "instrument_key")]
            for position in open_positions
        ],
    }


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

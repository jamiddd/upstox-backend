from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions import AppConfigError, UpstoxApiError
from app.services.instrument_rules_service import InstrumentRulesService, slice_quantity_for_freeze
from app.services.order_book_lookup import TERMINAL_ORDER_STATUSES, index_orders_by_id, order_status
from app.services.pending_oco_pairs_store import OcoPair, PendingOcoPairsStore
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

    async def place_market_bracket_order(
        self,
        access_token: str,
        *,
        instrument_key: str,
        transaction_type: str,
        quantity: int,
        product: str,
        target_trigger_price: float,
        stoploss_trigger_price: float,
        slice_quantity: int,
        pending_oco_store: PendingOcoPairsStore,
    ) -> dict[str, Any]:
        """Places a real immediate-fill MARKET order for entry, then attaches target/stoploss
        exit legs the same way attach_gtt_exits does for an already-open position with no
        bracket -- see that method's own doc comment for why those are plain orders, not GTT.

        Unlike place_bracket_order, whose GTT ENTRY leg Upstox always executes as a LIMIT order at
        the trigger price -- a GTT order is *always* placed as a LIMIT order on execution, even
        with trigger_type=IMMEDIATE, per Upstox's own docs -- this is for the app's "MKT" order
        button, where pressing Buy/Sell Market must actually fill at market, not silently become a
        limit order.

        Only the quantity that actually got submitted as a market entry has exits attached -- a
        slice that fails to enter must not end up with a stray target/stoploss bracket for shares
        that were never bought.
        """
        slices = _split_quantity(quantity, slice_quantity)
        entry_slices: list[dict[str, Any]] = []
        for index, slice_qty in enumerate(slices, start=1):
            try:
                response = await self.upstox.place_market_order(
                    access_token,
                    instrument_key=instrument_key,
                    transaction_type=transaction_type,
                    quantity=slice_qty,
                    product=product,
                )
                entry_slices.append(
                    {
                        "slice_number": index,
                        "quantity": slice_qty,
                        "status": "success",
                        "upstox_response": response,
                    }
                )
            except UpstoxApiError as exc:
                entry_slices.append(
                    {
                        "slice_number": index,
                        "quantity": slice_qty,
                        "status": "error",
                        "error": str(exc),
                    }
                )

        entered_quantity = sum(slice_result["quantity"] for slice_result in entry_slices if slice_result["status"] == "success")
        exits: Optional[dict[str, Any]] = None
        if entered_quantity > 0:
            exit_transaction_type = "SELL" if transaction_type == "BUY" else "BUY"
            exits = await self.attach_gtt_exits(
                access_token,
                instrument_key=instrument_key,
                quantity=entered_quantity,
                product=product,
                exit_transaction_type=exit_transaction_type,
                target_trigger_price=target_trigger_price,
                stoploss_trigger_price=stoploss_trigger_price,
                slice_quantity=slice_quantity,
                pending_oco_store=pending_oco_store,
            )

        entry_all_succeeded = all(slice_result["status"] == "success" for slice_result in entry_slices)
        exits_succeeded = exits is None or exits["status"] == "success"
        if entered_quantity == 0:
            overall_status = "error"
        elif entry_all_succeeded and exits_succeeded:
            overall_status = "success"
        else:
            overall_status = "partial_success"

        return {
            "status": overall_status,
            "source": "upstox_market_with_gtt_exits",
            "total_quantity": quantity,
            "entered_quantity": entered_quantity,
            "slice_quantity": slice_quantity,
            "slice_count": len(slices),
            "entry_slices": entry_slices,
            "exits": exits,
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
        slice_quantity: int,
        pending_oco_store: PendingOcoPairsStore,
    ) -> dict[str, Any]:
        """Attaches a target and a stoploss to an already-open position that has no existing GTT
        bracket, without touching the entry.

        FIX: this used to place two independent Upstox GTT type=SINGLE orders per slice (one rule
        each), specifically to avoid place_bracket_order's type=MULTIPLE shape -- MULTIPLE always
        includes its own ENTRY rule (entry_trigger_type IMMEDIATE fires right away), which would
        submit a second live entry and double the position. That SINGLE-with-no-ENTRY shape was
        never actually valid: Upstox's GTT API requires exactly one ENTRY rule in *every* GTT
        order regardless of type, so every one of these calls was rejected outright with "One
        ENTRY strategy is required." -- 100% of the time, not an edge case.

        There is no GTT shape that attaches only exits to an already-open position without also
        re-firing a live entry (the ENTRY rule Upstox requires always executes once its condition
        is met, and for an already-open position that condition is already true). So this now
        places two independent plain (non-GTT) orders per slice instead -- a LIMIT order for the
        target, an SL-M order for the stoploss -- which carry no OCO (one-cancels-other) guarantee
        on their own. Each successfully-placed pair is registered in [pending_oco_store] so
        `oco_watcher`'s background loop can cancel whichever leg doesn't fill once the other does.

        [exit_transaction_type] is the *exit* side (opposite of how the position was opened --
        e.g. "SELL" to exit a long), since a plain order has no ENTRY leg to infer direction from.
        [quantity] is sliced by [slice_quantity] the same way place_bracket_order slices its own
        entry -- a position sized over the instrument's freeze quantity would otherwise have both
        legs rejected outright by Upstox.

        Best-effort per leg per slice, same as exit_positions: one leg/slice failing doesn't stop
        the rest. A slice's pair is only registered for OCO watching once *both* legs place
        successfully -- a lone surviving leg (the other failed) isn't a pair to reconcile, it's
        just a resting order the caller's own partial_success/error reporting already surfaces.
        Returns "success" (every leg of every slice placed), "partial_success" (some placed), or
        "error" (none placed).
        """
        slices = _split_quantity(quantity, slice_quantity)
        placed_slices: list[dict[str, Any]] = []
        for index, slice_qty in enumerate(slices, start=1):
            leg_results: dict[str, dict[str, Any]] = {}
            for key, order_type, price, trigger_price in (
                ("target", "LIMIT", target_trigger_price, 0.0),
                ("stoploss", "SL-M", 0.0, stoploss_trigger_price),
            ):
                order = {
                    "quantity": slice_qty,
                    "product": product,
                    "order_type": order_type,
                    "price": price,
                    "trigger_price": trigger_price,
                    "instrument_token": instrument_key,
                    "transaction_type": exit_transaction_type,
                }
                try:
                    response = await self.upstox.place_order(
                        access_token,
                        instrument_key=instrument_key,
                        transaction_type=exit_transaction_type,
                        quantity=slice_qty,
                        product=product,
                        order_type=order_type,
                        price=price,
                        trigger_price=trigger_price,
                    )
                    leg_results[key] = {
                        "status": "success",
                        "submitted_order": order,
                        "upstox_response": response,
                        "order_id": _extract_order_id(response),
                    }
                except UpstoxApiError as exc:
                    leg_results[key] = {
                        "status": "error",
                        "submitted_order": order,
                        "error": str(exc),
                    }
            placed_slices.append(
                {
                    "slice_number": index,
                    "quantity": slice_qty,
                    "target": leg_results["target"],
                    "stoploss": leg_results["stoploss"],
                }
            )
            target_order_id = leg_results["target"].get("order_id")
            stoploss_order_id = leg_results["stoploss"].get("order_id")
            if target_order_id and stoploss_order_id:
                pending_oco_store.add(
                    OcoPair(
                        target_order_id=target_order_id,
                        stoploss_order_id=stoploss_order_id,
                        instrument_key=instrument_key,
                    )
                )

        successes = sum(
            1
            for slice_result in placed_slices
            for leg in ("target", "stoploss")
            if slice_result[leg]["status"] == "success"
        )
        total_legs = len(placed_slices) * 2
        overall_status = "success" if successes == total_legs else "partial_success" if successes else "error"
        return {
            "status": overall_status,
            "total_quantity": quantity,
            "slice_quantity": slice_quantity,
            "slice_count": len(slices),
            "slices": placed_slices,
        }

    async def get_gtt_orders_for_instrument(
        self, access_token: str, *, instrument_key: str, include_history: bool = False
    ) -> list[dict[str, Any]]:
        """GTT orders for one instrument.

        By default, "active" only -- excludes terminal statuses (cancelled/rejected/completed);
        anything else is treated as still-live so an unfamiliar status fails open rather than
        silently hiding a real order. Lets the app find the bracket order behind an open position
        so its target/stoploss can be edited (see modify_gtt_bracket).

        With include_history=True, COMPLETED brackets are also returned (still excludes
        cancelled/rejected, which never actually fired) -- lets the app show what target/stoploss
        was active for a now-closed position, by matching a specific order's fill time against
        each returned GTT's own `created_at`.
        """
        payload = await self.upstox.get_gtt_orders(access_token)
        data = payload.get("data")
        orders = data if isinstance(data, list) else []
        excluded_statuses = _TERMINAL_GTT_STATUSES if not include_history else _NEVER_FIRED_GTT_STATUSES
        return [
            order
            for order in orders
            if isinstance(order, dict)
            and order.get("instrument_token") == instrument_key
            and str(order.get("status", "")).upper() not in excluded_statuses
        ]

    async def get_pending_exit_orders(
        self,
        access_token: str,
        *,
        instrument_key: str,
        pending_oco_store: PendingOcoPairsStore,
    ) -> list[dict[str, Any]]:
        """Plain target/stoploss order pairs attach_gtt_exits placed for one instrument, still
        pending reconciliation by `oco_watcher` -- lets the app find the exit orders behind an
        open position that has no GTT bracket, so they can be shown and modified (via
        OrderModificationService.modify_orders), the same way get_gtt_orders_for_instrument lets
        it find/modify a GTT bracket. Without this, GttEditDialog only ever finds a GTT bracket,
        never a plain-order pair -- reopening it for a position protected this way looked
        identical to "no protection yet" and would attach a second, redundant pair on top of the
        first.

        A pair is only returned once both legs are confirmed still live in the order book -- one
        that's already reached a terminal status (filled/cancelled/rejected) is stale and about to
        be dropped by oco_watcher's own next tick, not something to offer for editing.
        """
        pairs = [pair for pair in pending_oco_store.load() if pair.instrument_key == instrument_key]
        if not pairs:
            return []

        order_book_payload = await self.upstox.get_order_book(access_token)
        orders_by_id = index_orders_by_id(order_book_payload)

        results: list[dict[str, Any]] = []
        for pair in pairs:
            target_order = orders_by_id.get(pair.target_order_id)
            stoploss_order = orders_by_id.get(pair.stoploss_order_id)
            if target_order is None or stoploss_order is None:
                continue
            if (
                order_status(target_order) in TERMINAL_ORDER_STATUSES
                or order_status(stoploss_order) in TERMINAL_ORDER_STATUSES
            ):
                continue
            results.append(
                {
                    "target_order_id": pair.target_order_id,
                    "stoploss_order_id": pair.stoploss_order_id,
                    "quantity": target_order.get("quantity"),
                    "product": target_order.get("product"),
                    "target_trigger_price": target_order.get("price"),
                    "stoploss_trigger_price": stoploss_order.get("trigger_price"),
                }
            )
        return results

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

    async def exit_all_positions(
        self, access_token: str, *, instrument_rules_service: InstrumentRulesService
    ) -> dict[str, Any]:
        """Flattens every currently open position -- backs the app's max-loss auto square-off
        (see MainViewModel.watchMaxLoss). Thin wrapper over exit_positions with no filter.
        """
        return await self.exit_positions(access_token, instrument_rules_service=instrument_rules_service)

    async def exit_positions(
        self,
        access_token: str,
        *,
        instrument_keys: Optional[list[str]] = None,
        instrument_rules_service: InstrumentRulesService,
    ) -> dict[str, Any]:
        """Flattens open positions (quantity != 0) with an immediate market order in the opposite
        direction. [instrument_keys] is None means every open position (exit_all_positions above);
        otherwise only positions whose instrument_token is in that set are closed -- e.g. "close
        only profitable positions", where the app itself decides which instrument_keys qualify (it
        already has live P&L from the WebSocket feed; the backend doesn't need to re-derive it
        from its own snapshot). Best-effort: one position failing to exit doesn't stop the others
        -- every attempted position's own result (success or error) is returned so the caller/UI
        can show exactly what happened to each one.

        Each position's own flattening order is sliced by its instrument's freeze quantity (same
        `_split_quantity`/`slice_quantity_for_freeze` machinery place_bracket_order uses) -- this
        backs the max-loss safety trigger, so a position sized over freeze quantity must not
        silently fail to flatten just because it was submitted as one oversized order.
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
            abs_quantity = int(abs(quantity))
            try:
                rules = await instrument_rules_service.get_rules(instrument_key)
                slice_qty = slice_quantity_for_freeze(abs_quantity, rules)
            except AppConfigError:
                # Best-effort: an instrument-rules lookup hiccup shouldn't block flattening a
                # position outright -- fall back to a single order (correct for the overwhelming
                # majority of positions, which are already under freeze quantity anyway).
                slice_qty = abs_quantity
            try:
                upstox_response: Any = None
                for chunk_quantity in _split_quantity(abs_quantity, slice_qty):
                    upstox_response = await self.upstox.place_market_order(
                        access_token,
                        instrument_key=instrument_key,
                        transaction_type=transaction_type,
                        quantity=chunk_quantity,
                        product=product,
                    )
                results.append(
                    {
                        "instrument_key": instrument_key,
                        "transaction_type": transaction_type,
                        "quantity": abs_quantity,
                        "status": "success",
                        "upstox_response": upstox_response,
                    }
                )
            except UpstoxApiError as exc:
                # A slice failing partway through is reported as this position's own "error", not
                # a partial-success this response shape has no room for -- a half-flattened
                # position isn't actually safe, so it should surface exactly like a total failure
                # (the app's own "close manually" messaging), not read as done.
                results.append(
                    {
                        "instrument_key": instrument_key,
                        "transaction_type": transaction_type,
                        "quantity": abs_quantity,
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
_TERMINAL_GTT_STATUSES = {"CANCELLED", "REJECTED", "COMPLETED"}

# Statuses that mean the bracket's rules never actually fired -- excluded even from history
# lookups since they don't represent real target/stoploss levels a position ever had. COMPLETED
# is kept for history since it means a rule genuinely triggered.
_NEVER_FIRED_GTT_STATUSES = {"CANCELLED", "REJECTED"}


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


def _extract_order_id(place_order_response: dict[str, Any]) -> Optional[str]:
    """Pulls the new order's id out of a Place Order V3 response (`{"data": {"order_id":
    "..."}}`) -- see SmartOrderService.attach_gtt_exits, which needs each leg's own order id to
    register the pair for OCO watching. Returns None for any unexpected shape rather than raising
    -- a leg that placed successfully but whose id couldn't be read just doesn't get OCO-watched,
    same "degrade, don't crash" posture as the rest of this service's best-effort semantics.
    """
    data = place_order_response.get("data")
    if not isinstance(data, dict):
        return None
    order_id = data.get("order_id")
    return order_id if isinstance(order_id, str) else None

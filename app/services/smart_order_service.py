from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.core.exceptions import AppConfigError, UpstoxApiError
from app.services.instrument_rules_service import InstrumentRulesService, slice_quantity_for_freeze
from app.services.order_book_lookup import TERMINAL_ORDER_STATUSES, index_orders_by_id, order_status
from app.services.pending_oco_pairs_store import PendingExit, PendingOcoPairsStore
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

        FIX: this used to place two independent GTT type=SINGLE orders per slice, which Upstox
        rejected outright (every GTT order requires exactly one ENTRY rule, which a SINGLE-with-
        no-ENTRY shape never had -- "One ENTRY strategy is required.", 100% of the time). Switching
        both legs to plain (non-GTT) orders fixed *that* rejection, but hit a second, harder wall:
        Upstox rejects the *second* live SELL order for the same already-held quantity outright.
        Placing a LIMIT sell for the full position reserves all of it against that order; a second
        SELL order for the same quantity (the SL-M stoploss) then has nothing left to "cover" it,
        so Upstox margin-checks it as a brand new naked short (the "You need to add Rs. X in your
        account" rejection) -- there is no way to have two live sell orders each covering the full
        position at once.

        So only the stoploss is ever placed as a real order now -- it's the safety-critical leg.
        The target is armed as a price level `oco_watcher`'s background loop itself watches
        against live quotes, and only becomes a real MARKET order once price actually crosses it
        (see that module's own doc comment, and [PendingExit] for the full reasoning). This trades
        the target leg's precision for correctness: it fires as a market order once the watcher's
        own tick notices the cross, not as a resting limit order sitting at the exact price, so a
        fast-moving price can slip a little between the cross and the fill.

        [exit_transaction_type] is the *exit* side (opposite of how the position was opened --
        e.g. "SELL" to exit a long). [quantity] is sliced by [slice_quantity] the same way
        place_bracket_order slices its own entry -- a position sized over the instrument's freeze
        quantity would otherwise be rejected outright by Upstox.

        Best-effort per slice, same as exit_positions: one slice's stoploss failing to place
        doesn't stop the rest. Returns "success" (every slice's stoploss placed), "partial_success"
        (some placed), or "error" (none placed). The response still carries a "target" sub-object
        per slice (mirroring "stoploss") purely so the app's existing per-leg error rendering
        keeps working unchanged -- there's no separate placement outcome for "target" any more
        since it was never a live order to begin with.
        """
        slices = _split_quantity(quantity, slice_quantity)
        placed_slices: list[dict[str, Any]] = []
        for index, slice_qty in enumerate(slices, start=1):
            order = {
                "quantity": slice_qty,
                "product": product,
                "order_type": "SL-M",
                "price": 0.0,
                "trigger_price": stoploss_trigger_price,
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
                    order_type="SL-M",
                    price=0.0,
                    trigger_price=stoploss_trigger_price,
                )
                stoploss_order_id = _extract_order_id(response)
                leg_result = {
                    "status": "success",
                    "submitted_order": order,
                    "upstox_response": response,
                }
                if stoploss_order_id:
                    pending_oco_store.add(
                        PendingExit(
                            stoploss_order_id=stoploss_order_id,
                            instrument_key=instrument_key,
                            exit_transaction_type=exit_transaction_type,
                            quantity=slice_qty,
                            product=product,
                            target_trigger_price=target_trigger_price,
                        )
                    )
            except UpstoxApiError as exc:
                leg_result = {
                    "status": "error",
                    "submitted_order": order,
                    "error": str(exc),
                }
            placed_slices.append(
                {
                    "slice_number": index,
                    "quantity": slice_qty,
                    "target": leg_result,
                    "stoploss": leg_result,
                }
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
        """Pending exits (see [PendingExit]) attach_gtt_exits armed for one instrument, still
        pending reconciliation by `oco_watcher` -- lets the app find the exit behind an open
        position that has no GTT bracket, so it can be shown and modified, the same way
        get_gtt_orders_for_instrument lets it find/modify a GTT bracket. Without this,
        GttEditDialog only ever finds a GTT bracket, never a pending exit -- reopening it for a
        position protected this way looked identical to "no protection yet" and would attach a
        second, redundant one on top of the first.

        [target_trigger_price] comes straight from the stored [PendingExit] (it was never a live
        order to read a current value back from -- see that class's own doc comment); only
        [stoploss_trigger_price] is read from the live order book. A pending exit is only returned
        once its stoploss is confirmed still live -- one that's already reached a terminal status
        (filled/cancelled/rejected) is stale and about to be dropped by oco_watcher's own next
        tick, not something to offer for editing.
        """
        pending_exits = [
            pending_exit
            for pending_exit in pending_oco_store.load()
            if pending_exit.instrument_key == instrument_key
        ]
        if not pending_exits:
            return []

        order_book_payload = await self.upstox.get_order_book(access_token)
        orders_by_id = index_orders_by_id(order_book_payload)

        results: list[dict[str, Any]] = []
        for pending_exit in pending_exits:
            stoploss_order = orders_by_id.get(pending_exit.stoploss_order_id)
            if stoploss_order is None or order_status(stoploss_order) in TERMINAL_ORDER_STATUSES:
                continue
            results.append(
                {
                    "stoploss_order_id": pending_exit.stoploss_order_id,
                    "quantity": pending_exit.quantity,
                    "product": pending_exit.product,
                    "target_trigger_price": pending_exit.target_trigger_price,
                    "stoploss_trigger_price": stoploss_order.get("trigger_price"),
                }
            )
        return results

    @staticmethod
    def update_pending_exit_target_price(
        *,
        instrument_key: str,
        stoploss_order_id: str,
        target_trigger_price: float,
        pending_oco_store: PendingOcoPairsStore,
    ) -> bool:
        """Re-points a pending exit's stored target price -- the counterpart, for the "no GTT
        bracket" case, of modifying a live GTT bracket's target rule. Doesn't touch Upstox at all
        (the target was never a live order -- see [PendingExit]'s own doc comment); the stoploss
        leg's own price is still modified through the ordinary `PUT /orders/modify` endpoint since
        it *is* a real order. [instrument_key] isn't strictly needed to identify the entry
        (stoploss_order_id alone is unique) but is required anyway so a caller can't accidentally
        repoint a different instrument's pending exit by guessing/reusing a stale order id.
        Returns whether a matching pending exit was actually found.
        """
        matching = any(
            pending_exit.instrument_key == instrument_key and pending_exit.stoploss_order_id == stoploss_order_id
            for pending_exit in pending_oco_store.load()
        )
        if not matching:
            return False
        return pending_oco_store.update_target_trigger_price(
            stoploss_order_id=stoploss_order_id,
            target_trigger_price=target_trigger_price,
        )

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
        self,
        access_token: str,
        *,
        instrument_rules_service: InstrumentRulesService,
        pending_oco_store: PendingOcoPairsStore,
    ) -> dict[str, Any]:
        """Flattens every currently open position -- backs the app's max-loss auto square-off
        (see MainViewModel.watchMaxLoss). Thin wrapper over exit_positions with no filter.
        """
        return await self.exit_positions(
            access_token,
            instrument_rules_service=instrument_rules_service,
            pending_oco_store=pending_oco_store,
        )

    async def exit_positions(
        self,
        access_token: str,
        *,
        instrument_keys: Optional[list[str]] = None,
        instrument_rules_service: InstrumentRulesService,
        pending_oco_store: PendingOcoPairsStore,
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

        # A still-active plain stoploss order (see attach_gtt_exits) reserves exit-side quantity
        # against its position -- flattening with a *fresh* market order on top of that makes
        # Upstox see more pending exposure than the position actually holds and reject it demanding
        # margin for a "naked" excess (the same rejection OrderPlacementViewModel.
        # cancelExistingExitOrdersIfNeeded exists to prevent for a manual single-position close).
        # Best-effort and never blocks flattening below: a failed lookup/cancel here just means
        # the market order that follows fails exactly the way it did before this existed.
        target_instrument_keys = {
            _string_value(item, "instrument_token", "instrument_key") for item in open_positions
        }
        await self._cancel_resting_stoploss_orders(
            access_token,
            instrument_keys=target_instrument_keys,
            pending_oco_store=pending_oco_store,
        )

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

    async def _cancel_resting_stoploss_orders(
        self,
        access_token: str,
        *,
        instrument_keys: set[Optional[str]],
        pending_oco_store: PendingOcoPairsStore,
    ) -> None:
        """Cancels whichever plain (non-GTT) stoploss orders -- see [PendingExit]/attach_gtt_exits
        -- are still resting for [instrument_keys] before [exit_positions] flattens them. Only the
        plain-order mechanism is handled: a real GTT bracket has no equivalent standalone stoploss
        order to cancel, and Upstox itself cleans up a GTT bracket's remaining legs once the
        position it was watching is actually flattened.

        A cancelled stoploss's own paired pending target is cleaned up server-side by
        `oco_watcher` on its own next tick, nothing to do here beyond the cancel itself.
        """
        pending_exits = [
            pending_exit
            for pending_exit in pending_oco_store.load()
            if pending_exit.instrument_key in instrument_keys
        ]
        if not pending_exits:
            return

        order_book_payload = await self.upstox.get_order_book(access_token)
        orders_by_id = index_orders_by_id(order_book_payload)

        cancelled_any = False
        for pending_exit in pending_exits:
            stoploss_order = orders_by_id.get(pending_exit.stoploss_order_id)
            if stoploss_order is None or order_status(stoploss_order) in TERMINAL_ORDER_STATUSES:
                continue
            try:
                await self.upstox.cancel_order(access_token, pending_exit.stoploss_order_id)
                cancelled_any = True
            except UpstoxApiError:
                continue

        if cancelled_any:
            # Gives Upstox a moment to actually release the quantity/margin the cancelled
            # order(s) were holding before the flattening market order below asks for it -- see
            # OrderPlacementViewModel.cancelExistingExitOrdersIfNeeded's own doc comment for why
            # this delay matters, not just the cancel itself.
            await asyncio.sleep(_EXIT_ORDER_CANCEL_SETTLE_SECONDS)


# How long to wait after cancelling a resting stoploss order before submitting the flattening
# market order that follows -- see _cancel_resting_stoploss_orders's own doc comment.
_EXIT_ORDER_CANCEL_SETTLE_SECONDS = 0.8

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

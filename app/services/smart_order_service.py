from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.core.exceptions import AppConfigError, UpstoxApiError
from app.services.gtt_history_store import GttHistoryStore
from app.services.instrument_rules_service import InstrumentRulesService, slice_quantity_for_freeze
from app.services.upstox_service import UpstoxService

_EXIT_MAX_ATTEMPTS = 3
_EXIT_RETRY_DELAY_SECONDS = 1.0


class SmartOrderService:
    """Translate app smart-order requests into Upstox GTT orders."""

    def __init__(self, upstox_service: UpstoxService, history_store: Optional[GttHistoryStore] = None) -> None:
        self.upstox = upstox_service
        # Optional -- every production call site passes one (see routes.py), but existing tests
        # construct SmartOrderService(fake_upstox) without one and shouldn't have to change just
        # to keep working. None means place/modify simply skip the direct-write hook below (falls
        # back to the old archive()-on-read behavior for that call), rather than erroring.
        self.history_store = history_store

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
            # Persist directly, right here, the moment Upstox's own place response confirms the
            # order -- not as a side effect of some later GET /orders/gtt call happening to
            # include it in its list (see GttHistoryStore's own doc comment for why that's the
            # actual bug this fixes). A partial multi-slice failure still leaves every slice that
            # DID place recorded, since this runs per-slice before the loop can hit a later error.
            if self.history_store is not None:
                for gtt_order_id in _extract_gtt_order_ids(response):
                    self.history_store.record_placed(
                        gtt_order_id,
                        instrument_key,
                        {
                            "gtt_order_id": gtt_order_id,
                            "instrument_token": instrument_key,
                            "quantity": slice_qty,
                            "product": product,
                            "status": "ACTIVE",
                            "rules": upstox_order["rules"],
                        },
                    )

        return {
            "status": "success",
            "source": "upstox_gtt",
            "total_quantity": quantity,
            "slice_quantity": slice_quantity,
            "slice_count": len(slices),
            "slices": placed_slices,
        }

    async def get_all_gtt_orders(self, access_token: str) -> list[dict[str, Any]]:
        """Every GTT order Upstox currently reports, status-normalized, with no instrument or
        status filtering applied.

        This is the unfiltered feed a durable local archive (see GttHistoryStore) needs to stay
        accurate -- get_gtt_orders_for_instrument's default view deliberately drops terminal
        statuses (cancelled/rejected/completed), so an archive built only from its output would
        never observe an order's transition into one of those and would keep serving its last
        pre-transition status forever.
        """
        payload = await self.upstox.get_gtt_orders(access_token)
        data = payload.get("data")
        orders = data if isinstance(data, list) else []
        return [
            {**order, "status": _gtt_order_status(order)}
            for order in orders
            if isinstance(order, dict)
        ]

    @staticmethod
    def filter_gtt_orders(
        normalized_orders: list[dict[str, Any]],
        *,
        instrument_key: str | None = None,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        """Narrow an already-normalized order list (see get_all_gtt_orders) to one instrument
        and/or a status view.

        By default, "active" only -- excludes terminal statuses (cancelled/rejected/completed);
        anything else is treated as still-live so an unfamiliar status fails open rather than
        silently hiding a real order. Lets the app find the bracket order behind an open position
        so its target/stoploss can be edited (see modify_gtt_bracket).

        With include_history=True, COMPLETED brackets are also returned (still excludes
        cancelled/rejected, which never actually fired) -- lets the app show what target/stoploss
        was active for a now-closed position, by matching a specific order's fill time against
        each returned GTT's own `created_at`.
        """
        excluded_statuses = _TERMINAL_GTT_STATUSES if not include_history else _NEVER_FIRED_GTT_STATUSES
        return [
            order
            for order in normalized_orders
            if (instrument_key is None or order.get("instrument_token") == instrument_key)
            and str(order.get("status", "")).upper() not in excluded_statuses
        ]

    async def get_gtt_orders_for_instrument(
        self, access_token: str, *, instrument_key: str | None = None, include_history: bool = False
    ) -> list[dict[str, Any]]:
        """GTT orders, optionally filtered to one instrument, narrowed by filter_gtt_orders (see
        that method for the status rules).

        Reads from this backend's own persistent record (GttHistoryStore) when one is configured
        -- no live Upstox call, and reliable even when Upstox's own list endpoint is having one of
        its unreliable moments (see GttHistoryStore's own doc comment for why that matters). Falls
        back to a live Upstox fetch (the old behavior) only when this instance has no
        history_store, e.g. a SmartOrderService constructed purely for the max-loss watcher's own
        stray-GTT cleanup role.
        """
        if self.history_store is not None:
            normalized_orders = self.history_store.list()
        else:
            normalized_orders = await self.get_all_gtt_orders(access_token)
        return self.filter_gtt_orders(
            normalized_orders, instrument_key=instrument_key, include_history=include_history
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
        instrument_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Re-points an existing GTT bracket's target/stoploss. The entry rule is resent
        unchanged (already triggered, since this only ever runs against an open position) --
        Upstox's GTT modify contract expects the full rule set, not a partial patch.

        instrument_key is optional purely for backward call-site compatibility -- pass it so the
        successful modify can be persisted directly (see GttHistoryStore.record_modified); without
        it the store write is skipped (falls back to the old on-next-list-read behavior for this
        one order).
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
        response = await self.upstox.modify_gtt_order(access_token, upstox_order)
        # Persist directly, right here, the moment Upstox's modify response confirms it -- same
        # "no dependency on a later list call" reasoning as place_bracket_order above.
        if self.history_store is not None and instrument_key:
            self.history_store.record_modified(
                gtt_order_id,
                instrument_key,
                {
                    "gtt_order_id": gtt_order_id,
                    "instrument_token": instrument_key,
                    "quantity": quantity,
                    "product": product,
                    "status": "ACTIVE",
                    "rules": rules,
                },
            )
        return response

    async def exit_all_positions(
        self,
        access_token: str,
        *,
        instrument_rules_service: InstrumentRulesService,
    ) -> dict[str, Any]:
        """Flattens every currently open position -- backs both the app's own max-loss auto
        square-off (MainViewModel.checkMaxLoss) and the backend's own max-loss watcher
        (max_loss_watcher.py). Thin wrapper over exit_positions with no filter.
        """
        return await self.exit_positions(
            access_token,
            instrument_rules_service=instrument_rules_service,
        )

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

        A position opened via place_bracket_order carries its target/stoploss as a separate GTT
        order that Upstox tracks independently of the position itself -- this flattening market
        order doesn't touch it. Left alone, that GTT stays armed against an instrument this account
        no longer holds a position in, and could still fire later (a stray, unintended order, not
        just a client display artifact -- the web client was found showing a closed position's
        stale TARGET/STOP lines because of exactly this). So once a position's flatten succeeds,
        its still-active bracket GTT(s) are cancelled too, best-effort (see _cancel_stray_gtts).
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
                        upstox_response = await self.upstox.place_market_order(
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
                refreshed_payload = await self.upstox.get_positions(access_token)
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

        flattened_keys = [
            key for key, result in result_by_key.items() if result.get("status") == "success"
        ]
        if flattened_keys:
            await self._cancel_stray_gtts(access_token, flattened_keys)

        return {
            "status": "success",
            "positions_found": len(open_positions),
            "results": [
                result_by_key[_string_value(position, "instrument_token", "instrument_key")]
                for position in open_positions
            ],
        }

    async def _cancel_stray_gtts(self, access_token: str, instrument_keys: list[str]) -> None:
        """Cancels every still-active bracket GTT for each just-flattened instrument.

        Best-effort and non-fatal by design -- exit_positions' own job (getting the position to
        flat) already succeeded by the time this runs; a stray GTT that fails to cancel here is a
        follow-up cleanup concern, not a reason to report the exit itself as failed. Runs one
        instrument at a time (not concurrently) since it's already piggybacking on a manual
        close/max-loss-trigger, not a latency-sensitive path.
        """
        for instrument_key in instrument_keys:
            try:
                orders = await self.get_gtt_orders_for_instrument(
                    access_token, instrument_key=instrument_key
                )
            except UpstoxApiError:
                continue
            for order in orders:
                gtt_order_id = order.get("gtt_order_id")
                if not isinstance(gtt_order_id, str) or not gtt_order_id:
                    continue
                try:
                    await self.upstox.cancel_gtt_order(access_token, gtt_order_id)
                except UpstoxApiError:
                    continue
                if self.history_store is not None:
                    self.history_store.record_cancelled(gtt_order_id)


# Terminal GTT statuses -- anything else (e.g. a still-pending/triggered rule) is treated as
# active. See SmartOrderService.get_gtt_orders_for_instrument.
_TERMINAL_GTT_STATUSES = {"CANCELLED", "REJECTED", "COMPLETED", "EXPIRED"}

# Statuses that mean the bracket's rules never actually fired -- excluded even from history
# lookups since they don't represent real target/stoploss levels a position ever had. COMPLETED
# is kept for history since it means a rule genuinely triggered.
_NEVER_FIRED_GTT_STATUSES = {"CANCELLED", "REJECTED"}

_ACTIVE_GTT_RULE_STATUSES = {"SCHEDULED", "TRIGGERED", "OPEN", "PENDING"}


def _gtt_order_status(order: dict[str, Any]) -> str:
    """Normalizes Upstox's rule-level GTT lifecycle into one order status.

    The V3 response documents status on each rule, not on the containing GTT. Older fixtures and
    some responses do include a top-level status, so preserve it when present. Otherwise any live
    rule keeps the bracket ACTIVE; a cancelled ENTRY with only inactive siblings is CANCELLED.
    Unknown combinations fail open as ACTIVE so a genuinely live order is never silently hidden.
    """
    explicit = str(order.get("status") or "").upper()
    if explicit:
        return explicit

    rules = order.get("rules")
    rule_statuses = [
        str(rule.get("status") or "").upper()
        for rule in rules if isinstance(rule, dict)
    ] if isinstance(rules, list) else []
    if any(status in _ACTIVE_GTT_RULE_STATUSES for status in rule_statuses):
        return "ACTIVE"
    if "CANCELLED" in rule_statuses:
        return "CANCELLED"
    if "FAILED" in rule_statuses or "REJECTED" in rule_statuses:
        return "REJECTED"
    if "EXPIRED" in rule_statuses:
        return "EXPIRED"
    if rule_statuses and all(status in {"COMPLETED", "INACTIVE"} for status in rule_statuses):
        return "COMPLETED"
    return "ACTIVE"


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


def _extract_gtt_order_ids(place_gtt_response: dict[str, Any]) -> list[str]:
    """Pulls the id(s) out of Upstox's own place_gtt_order response, defensively.

    Upstox's real response shape is `{"status": "success", "data": {"gtt_order_ids": [...]}}` --
    plural, a list, even for a single-rule bracket (confirmed against docs/ORDER_PLACEMENT_API.md
    and tests/test_upstox_service.py's own fixtures) -- never assume a bare singular
    `gtt_order_id` key exists. Returns [] for any unexpected shape rather than raising, since a
    malformed response here shouldn't take down order placement itself (the order may well have
    still gone through on Upstox's side; this only affects whether *this* backend can track it).
    """
    data = place_gtt_response.get("data")
    if not isinstance(data, dict):
        return []
    ids = data.get("gtt_order_ids")
    if not isinstance(ids, list):
        return []
    return [order_id for order_id in ids if isinstance(order_id, str) and order_id]


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

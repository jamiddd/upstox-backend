from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Optional

from app.services.upstox_service import UpstoxService

# Upstox's order `tag` field is short/alphanumeric (observed limit: 20 chars) -- nowhere near
# enough to carry a full TriggerRule.id/ClientFallbackRule.id UUID (36 chars) directly. A
# deterministic hash-truncation gives a stable, collision-resistant-enough (2^64 space at 16 hex
# chars, plenty for one account's daily order volume), alphanumeric-safe (hex digits) tag that
# both a first placement and a later idempotent re-check derive identically from the same
# idempotency key, without the backend needing to persist a key->tag mapping anywhere.
_TAG_LENGTH = 16


def derive_order_tag(idempotency_key: str) -> str:
    """Deterministically derives an Upstox-safe order tag from an arbitrary idempotency key
    (always a `TriggerRule.id`/`ClientFallbackRule.id` UUID from the Android client) -- see this
    module's own header comment for why a hash-truncation, not the raw key, is what actually goes
    to Upstox."""
    return sha256(idempotency_key.encode("utf-8")).hexdigest()[:_TAG_LENGTH]


@dataclass
class OrderEnginePlacementResult:
    """[already_existed] distinguishes "found and returned a prior order for this idempotency
    key" from "genuinely placed a new one" -- both are a *success* outcome to the caller (the
    self-hosted trigger engine's own retry/dedupe logic in TriggerExecutor/ClientFallbackEvaluator
    is what's supposed to call this more than once for the same key on an ambiguous prior
    attempt), this is purely for observability/logging, not a different HTTP response shape."""

    already_existed: bool
    order: dict[str, Any]


class OrderEngineOrderService:
    """The self-hosted trigger engine's (`docs/ORDER_POSITION_OVERHAUL_DESIGN.md` §6.3/§6.4) own
    real order-placement path -- deliberately new and separate from `SmartOrderService` (which
    only ever places/manages *GTT* orders, the exact mechanism Part 2 replaces) and from
    `place_smart_bracket_order`'s route, per this overhaul's isolation rule: new backend module,
    not an extension of the existing GTT-based order placement path.

    Every placement is idempotency-keyed via [derive_order_tag] -- see that function's own doc
    comment. [find_existing_order] is what `BrokerOrderGateway.findExistingOrder` (Android) is
    backed by; [place_order] itself also checks first internally before ever calling Upstox, so a
    caller that retries the exact same idempotency key (e.g. after a network timeout on the first
    attempt genuinely never reaching this server at all) is safe even without the Android-side
    Executor's own ambiguous-response handling catching it first.
    """

    def __init__(self, upstox: UpstoxService) -> None:
        self.upstox = upstox

    async def find_existing_order(
        self, access_token: str, idempotency_key: str
    ) -> Optional[dict[str, Any]]:
        """Looks up today's order book for an order whose tag matches [idempotency_key]'s derived
        tag. Upstox has no "get order by my custom key" endpoint -- the order book (today's full
        order list, tag included on every entry) is the only place this lookup can happen, so
        this fetches and filters client-side rather than a targeted call."""
        tag = derive_order_tag(idempotency_key)
        book = await self.upstox.get_order_book(access_token)
        orders = book.get("data") if isinstance(book, dict) else None
        if not isinstance(orders, list):
            return None
        for order in orders:
            if isinstance(order, dict) and order.get("tag") == tag:
                return order
        return None

    async def place_order(
        self,
        access_token: str,
        *,
        idempotency_key: str,
        instrument_key: str,
        transaction_type: str,
        quantity: int,
        product: str,
        order_type: str,
        price: float = 0,
        trigger_price: float = 0,
    ) -> OrderEnginePlacementResult:
        """Idempotent placement: returns the existing order for [idempotency_key] if one's
        already on today's order book, otherwise places a new one tagged with its derived tag.
        Lets `UpstoxApiError`/transport exceptions (timeouts, connection failures) propagate
        uncaught -- the route layer is what translates those into the Rejected-vs-Ambiguous
        distinction the Android `BrokerOrderGateway` contract needs (a 4xx from Upstox is a real
        rejection; a 5xx or transport-level failure is genuinely ambiguous, since the order may or
        may not have reached Upstox's own engine)."""
        existing = await self.find_existing_order(access_token, idempotency_key)
        if existing is not None:
            return OrderEnginePlacementResult(already_existed=True, order=existing)

        placed = await self.upstox.place_order(
            access_token,
            instrument_key=instrument_key,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            tag=derive_order_tag(idempotency_key),
        )
        return OrderEnginePlacementResult(already_existed=False, order=placed)

    async def cancel_order(
        self, access_token: str, idempotency_key: str
    ) -> Optional[dict[str, Any]]:
        """§7.7's "an actual broker order already in flight" cancel path: a real Upstox cancel
        call, then re-confirmed against broker state afterward rather than trusted from the cancel
        ack alone -- "confirmed against broker state afterward rather than trusted from the API
        response alone ... that race's outcome ... is surfaced as a fact, never treated as an
        error or silently swallowed." Returns `None` if [idempotency_key] has no matching order at
        all (nothing to cancel -- the route layer surfaces this as 404, not a cancel failure).
        Lets `UpstoxApiError`/transport exceptions propagate uncaught, same reasoning as
        [place_order]."""
        existing = await self.find_existing_order(access_token, idempotency_key)
        if existing is None:
            return None
        order_id = existing.get("order_id")
        await self.upstox.cancel_order(access_token, order_id)
        confirmed = await self.find_existing_order(access_token, idempotency_key)
        # A confirmed re-fetch that comes back empty (e.g. the cancelled order fell off today's
        # book entirely) still needs *something* to return -- the pre-cancel snapshot is the best
        # available fact in that edge case, never treated as "cancel silently failed."
        return confirmed if confirmed is not None else existing

    async def modify_order_quantity(
        self, access_token: str, idempotency_key: str, quantity: int
    ) -> Optional[dict[str, Any]]:
        """§7.7's quantity-only modify path -- "offered only on genuine LIMIT conditional
        entries ... the broker's native modify is called directly." Every other field
        (price/order_type/trigger_price/validity) is carried over unchanged from the order's own
        current broker-reported values, never re-supplied by the caller, so this can only ever
        change quantity, matching the design's own scoping. Returns `None` if [idempotency_key]
        has no matching order (route layer surfaces 404). Same confirm-after-mutate discipline as
        [cancel_order]."""
        existing = await self.find_existing_order(access_token, idempotency_key)
        if existing is None:
            return None
        await self.upstox.modify_order(
            access_token,
            {
                "order_id": existing.get("order_id"),
                "quantity": quantity,
                "validity": existing.get("validity", "DAY"),
                "price": existing.get("price", 0),
                "order_type": existing.get("order_type", "LIMIT"),
                "trigger_price": existing.get("trigger_price", 0),
            },
        )
        confirmed = await self.find_existing_order(access_token, idempotency_key)
        return confirmed if confirmed is not None else existing

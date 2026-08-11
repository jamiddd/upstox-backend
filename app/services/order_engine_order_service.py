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


class UnintendedShortGuardError(ValueError):
    """Raised by [OrderEngineOrderService.place_order] when [guard_against_unintended_short] is
    set and a `SELL` would exceed the instrument's actual currently-held long quantity -- see that
    parameter's own doc comment. A route layer catches this and maps it to a real, final
    rejection (422), same as a genuine broker-side rejection -- never ambiguous, since no broker
    call was even attempted."""


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

    async def resolve_signed_position_quantity(self, access_token: str, instrument_key: str) -> float:
        """Re-fetches Upstox's own real positions (never a cached/ledger value) and returns the
        raw signed net quantity for [instrument_key] -- positive for a long, negative for a short,
        `0.0` if flat or the instrument isn't in the positions response at all. The shared
        broker-truth primitive both [resolve_held_long_quantity] and
        [resolve_closeable_quantity] derive from."""
        payload = await self.upstox.get_positions(access_token)
        data = payload.get("data") if isinstance(payload, dict) else None
        positions = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        for position in positions:
            key = position.get("instrument_token") or position.get("instrument_key")
            if key != instrument_key:
                continue
            quantity = position.get("quantity")
            if isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
                return float(quantity)
            return 0.0
        return 0.0

    async def resolve_held_long_quantity(self, access_token: str, instrument_key: str) -> float:
        """The currently-held *long* quantity for [instrument_key] -- exactly the broker-truth
        re-confirmation [place_order]'s own `guard_against_unintended_short` needs. `0.0` if flat,
        absent, or already net-short (a negative broker-reported quantity means there's nothing
        long left to sell against -- floored at zero, never returned as a negative "held"
        amount)."""
        return max(await self.resolve_signed_position_quantity(access_token, instrument_key), 0.0)

    async def resolve_closeable_quantity(
        self, access_token: str, instrument_key: str, entry_transaction_type: str,
    ) -> float:
        """The quantity of [instrument_key] actually available at the broker to close a lot whose
        *entry* was [entry_transaction_type] -- `"BUY"` (a long lot, closes via `SELL`) reads the
        held-long magnitude; `"SELL"` (a short lot, closes via `BUY`-to-cover) reads the held-short
        magnitude. Used by `order_engine_max_loss_watcher.flatten_open_lots` to cap an emergency
        exit at what's genuinely resting at the broker rather than trusting the ledger's own
        (possibly stale) `remaining_quantity` blindly -- same broker-truth-over-cached-value
        discipline as [resolve_held_long_quantity], generalized to both directions since a
        flatten (unlike the manual-entry guard, which only ever fires on a `SELL`) can be closing
        either a long or a short lot."""
        signed = await self.resolve_signed_position_quantity(access_token, instrument_key)
        if entry_transaction_type.upper() == "SELL":
            return max(-signed, 0.0)
        return max(signed, 0.0)

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
        guard_against_unintended_short: bool = False,
    ) -> OrderEnginePlacementResult:
        """Idempotent placement: returns the existing order for [idempotency_key] if one's
        already on today's order book, otherwise places a new one tagged with its derived tag.
        Lets `UpstoxApiError`/transport exceptions (timeouts, connection failures) propagate
        uncaught -- the route layer is what translates those into the Rejected-vs-Ambiguous
        distinction the Android `BrokerOrderGateway` contract needs (a 4xx from Upstox is a real
        rejection; a 5xx or transport-level failure is genuinely ambiguous, since the order may or
        may not have reached Upstox's own engine).

        [guard_against_unintended_short]: when `True` and [transaction_type] is `"SELL"`, this
        assumes -- per explicit product direction -- that the caller intends to *close* an
        existing long, never to open or extend a short, and re-confirms [instrument_key]'s actual
        held long quantity via [resolve_held_long_quantity] before ever calling Upstox. A
        [quantity] exceeding what's actually held raises [UnintendedShortGuardError] rather than
        silently capping the order to a smaller amount or placing it anyway -- capping without
        telling the caller would place a different order than what was actually requested, which
        is its own kind of surprise; rejecting outright with a clear reason is the honest failure
        mode here. This flag is opt-in and defaults `False` -- every pre-existing caller
        (`TriggerExecutor`'s bracket-leg exits, `order_engine_max_loss_watcher.py`'s flatten) is
        unaffected; only the manual entry-order path (`EntryOrderPlacer`, Android) sets it."""
        if guard_against_unintended_short and transaction_type.upper() == "SELL":
            held = await self.resolve_held_long_quantity(access_token, instrument_key)
            if quantity > held:
                raise UnintendedShortGuardError(
                    f"Refusing to place a SELL of {quantity} for {instrument_key} -- only "
                    f"{held:g} is currently held long. This would open or extend a short "
                    "position, which this screen assumes is a mistake rather than intended."
                )

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

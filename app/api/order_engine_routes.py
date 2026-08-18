from __future__ import annotations

import logging
from typing import Any, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.dependencies import (
    get_journal_store,
    get_order_engine_ledger_store,
    get_order_engine_lot_tracker,
    get_token_store,
    get_upstox_service,
)
from app.core.config import Settings, get_settings
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.core.security import require_mobile_api_key
from app.services.instrument_rules_service import InstrumentRulesService
from app.services.journal_store import JournalStore
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker
from app.services.order_engine_order_service import (
    OrderEngineOrderService,
    UnintendedShortGuardError,
    derive_order_tag,
)
from app.services.order_history_recorder import OrderHistoryRecorder
from app.services import order_engine_trigger_evaluator
from app.services.position_flattener import flatten_positions
from app.services.token_store import EncryptedTokenStore
from app.services.trade_context_service import extract_order_ids
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

# A brand-new, dedicated router -- not added to routes.py's protected_router/dual_router, and not
# extending place_smart_bracket_order or any other existing order-placement route. This is Part
# 2's (`docs/ORDER_POSITION_OVERHAUL_DESIGN.md` §6.3/§6.4) own real order-placement path, replacing
# broker-side GTT triggering, so it deliberately doesn't reuse the GTT-based path it replaces.
# Mobile-only for now (require_mobile_api_key, not require_mobile_or_web) -- the new order engine
# is an Android-only, useNewOrderEngine-flagged effort; the web client has no reason to call this
# yet.
router = APIRouter(prefix="/order-engine", dependencies=[Depends(require_mobile_api_key)])


class OrderEnginePlaceOrderRequest(BaseModel):
    """Mirrors the Android `TriggerOrderPayload` shape decoded from `TriggerAction.payloadJson`/
    `ClientFallbackRule.action.payloadJson` -- see that class's own doc comment."""

    idempotency_key: str = Field(min_length=1)
    instrument_key: str = Field(min_length=1)
    transaction_type: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    product: Literal["I", "D", "MTF"] = "I"
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] = "MARKET"
    price: float = Field(default=0.0, ge=0)
    trigger_price: float = Field(default=0.0, ge=0)
    # See OrderEngineOrderService.place_order's own doc comment -- opt-in, defaults False so every
    # pre-existing caller (trigger-fired exits, max-loss flatten) is unaffected; only the manual
    # entry-order screen (Android's EntryOrderPlacer) sets this True.
    guard_against_unintended_short: bool = False
    # Part 5's entry-correlation fix (docs/ORDER_HISTORY_V2_DESIGN.md): optional, defaults None so
    # every pre-existing caller is unaffected. A caller that knows what this placement *is* (an
    # `EntryOrderPlacer`-style manual entry vs a `TriggerExecutor`-fired exit) should set this --
    # it's the one signal the server has no other way to recover once [idempotency_key]'s tag has
    # been derived (a one-way hash), needed so a later fill can correctly auto-create/update
    # `lots` instead of sitting unresolved in `order_history` forever.
    role: Optional[Literal["ENTRY", "EXIT", "MANUAL"]] = None
    # §6.3 Part B2: the bracket this placement itself intends -- only meaningful alongside
    # role="ENTRY" (Android's EntryOrderPlacer is the one caller that sets these). Carried through
    # record_placement onto this order's own order_history row so the server can arm the bracket
    # itself once the fill confirms (OrderHistoryRecorder._apply_entry_fill ->
    # order_engine_lot_bracket_armer.arm_lot_bracket), rather than waiting for the client's own
    # PUT /ledger/trigger-rules mirror -- see that route's own doc comment for why that mirror
    # alone left a disconnected phone's fresh entry completely unprotected. All `None` (every
    # pre-existing caller, and any EXIT/MANUAL placement) means no bracket to arm.
    target_price: Optional[float] = None
    stoploss_price: Optional[float] = None
    trailing_gap: Optional[float] = None


class OrderEnginePlaceOrderResponse(BaseModel):
    outcome: Literal["placed"] = "placed"
    broker_order_id: Optional[str] = None
    already_existed: bool = False
    raw: dict[str, Any]


class OrderEngineOrderStatusResponse(BaseModel):
    broker_order_id: Optional[str] = None
    status: Optional[str] = None
    raw: dict[str, Any]


class OrderEngineModifyQuantityRequest(BaseModel):
    """§7.7: quantity is the *only* field this route accepts -- price/order_type/trigger_price/
    validity are always carried over unchanged from the order's own current broker-reported
    values (see `OrderEngineOrderService.modify_order_quantity`), never taken from the caller."""

    quantity: int = Field(gt=0)


class OrderEngineCancelOrderResponse(BaseModel):
    outcome: Literal["cancelled"] = "cancelled"
    status: Optional[str] = None
    raw: dict[str, Any]


class OrderEngineModifyQuantityResponse(BaseModel):
    outcome: Literal["modified"] = "modified"
    status: Optional[str] = None
    raw: dict[str, Any]


def _http_error(status_code: int, message: str) -> HTTPException:
    """Same normalized `{"status": "error", "message": ...}` envelope routes.py's own
    `_http_error`/`_upstox_http_error` use -- matching it here (rather than a plain
    `HTTPException(status_code, message)`, which FastAPI would instead wrap as a bare
    `{"detail": message}` string) means Android's existing generic `ApiErrorBody` parsing already
    surfaces this route's real message with no route-specific parsing needed on that side."""
    return HTTPException(status_code=status_code, detail={"status": "error", "message": message})


def _upstox_call_error(exc: Exception) -> HTTPException:
    """Shared Rejected-vs-Ambiguous translation for a call already known to be against an
    *existing* broker order (cancel/modify) -- same reasoning as the inline try/except in
    [place_order_engine_order]/[get_order_engine_order] above, factored out here since cancel and
    modify both need the identical mapping and there's no placement-specific nuance left to keep
    them separate for."""
    if isinstance(exc, UpstoxApiError):
        if exc.status_code < 500:
            return _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.message)
        return _http_error(status.HTTP_502_BAD_GATEWAY, exc.message)
    return _http_error(status.HTTP_502_BAD_GATEWAY, "Could not reach Upstox")


def _load_access_token(token_store: EncryptedTokenStore) -> str:
    try:
        return token_store.load_access_token()
    except UpstoxAuthRequiredError as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except TokenStoreError as exc:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc


@router.post("/orders", response_model=OrderEnginePlaceOrderResponse)
async def place_order_engine_order(
    order: OrderEnginePlaceOrderRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEnginePlaceOrderResponse:
    """The real `BrokerOrderGateway.placeOrder` backing per §6.3 -- idempotency-keyed by
    [OrderEnginePlaceOrderRequest.idempotency_key] (always a `TriggerRule.id`/
    `ClientFallbackRule.id` on the Android side), via `OrderEngineOrderService`.

    HTTP status is the Rejected-vs-Ambiguous signal Android's `BrokerOrderGateway` needs: a 4xx
    here means Upstox genuinely rejected the order (bad margin, invalid instrument, etc.) -- safe
    to treat as a real, final rejection. A 502 means Upstox's own request either failed at the
    transport level (timeout/connection failure) or returned a 5xx of its own -- genuinely
    ambiguous, Android must call `GET /orders/{idempotency_key}` to confirm before ever retrying,
    exactly as `TriggerExecutor`'s existing ambiguous-response handling already does.
    """
    access_token = _load_access_token(token_store)
    order_service = OrderEngineOrderService(service)
    try:
        result = await order_service.place_order(
            access_token,
            idempotency_key=order.idempotency_key,
            instrument_key=order.instrument_key,
            transaction_type=order.transaction_type,
            quantity=order.quantity,
            product=order.product,
            order_type=order.order_type,
            price=order.price,
            trigger_price=order.trigger_price,
            guard_against_unintended_short=order.guard_against_unintended_short,
        )
    except UnintendedShortGuardError as exc:
        # A real, final rejection -- no broker call was even attempted, so there's nothing
        # ambiguous about it (unlike the UpstoxApiError/transport cases below).
        logger.warning("order-engine placement rejected -- unintended-short guard: %s", exc)
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except UpstoxApiError as exc:
        if exc.status_code < 500:
            logger.warning("order-engine placement rejected by Upstox: %s", exc.message)
            raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.message) from exc
        logger.warning("order-engine placement ambiguous -- Upstox returned %s: %s", exc.status_code, exc.message)
        raise _http_error(status.HTTP_502_BAD_GATEWAY, exc.message) from exc
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        # Never reached Upstox's own response handling at all -- the order may or may not have
        # landed. Same "ambiguous" outcome as an Upstox-side 5xx above, from Android's perspective.
        logger.warning("order-engine placement ambiguous -- transport failure: %s", exc)
        raise _http_error(status.HTTP_502_BAD_GATEWAY, "Could not reach Upstox") from exc

    broker_order_id = next(iter(extract_order_ids(result.order)), None)

    if order.role is not None and broker_order_id:
        # Best-effort bookkeeping -- the broker placement already succeeded above; a failure here
        # must never surface as a placement failure, only a lost opportunity to auto-correlate
        # this order's eventual fill (same posture the WS-push recorder's own try/except uses).
        try:
            OrderHistoryRecorder(ledger).record_placement(
                broker_order_id=broker_order_id,
                order_tag=derive_order_tag(order.idempotency_key),
                idempotency_key=order.idempotency_key,
                role=order.role,
                instrument_key=order.instrument_key,
                transaction_type=order.transaction_type,
                product=order.product,
                order_type=order.order_type,
                requested_quantity=order.quantity,
                requested_price=order.price,
                trigger_price=order.trigger_price,
                target_price=order.target_price,
                stoploss_price=order.stoploss_price,
                trailing_gap=order.trailing_gap,
            )
        except Exception:
            logger.warning(
                "order-engine placement-time order_history correlation write failed for %s",
                broker_order_id, exc_info=True,
            )

    return OrderEnginePlaceOrderResponse(
        broker_order_id=broker_order_id,
        already_existed=result.already_existed,
        raw=result.order,
    )


@router.get("/orders/{idempotency_key}", response_model=OrderEngineOrderStatusResponse)
async def get_order_engine_order(
    idempotency_key: str,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> OrderEngineOrderStatusResponse:
    """The real `BrokerOrderGateway.findExistingOrder` backing -- 404 means genuinely not found
    (not ambiguous; today's order book was successfully read and had no matching tag), so Android
    can safely treat 404 as "confirmed absent" per §6.3's "retry only if genuinely absent" rule.
    A failure to even read the order book itself surfaces as a real HTTP error (401/502), not a
    404 -- conflating "couldn't check" with "confirmed absent" would be exactly the unsafe
    assumption §6.3 warns against.
    """
    access_token = _load_access_token(token_store)
    order_service = OrderEngineOrderService(service)
    try:
        existing = await order_service.find_existing_order(access_token, idempotency_key)
    except UpstoxApiError as exc:
        raise _http_error(status.HTTP_502_BAD_GATEWAY, exc.message) from exc
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise _http_error(status.HTTP_502_BAD_GATEWAY, "Could not reach Upstox") from exc

    if existing is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No matching order found")

    return OrderEngineOrderStatusResponse(
        broker_order_id=existing.get("order_id"),
        status=existing.get("status"),
        raw=existing,
    )


@router.post("/orders/{idempotency_key}/cancel", response_model=OrderEngineCancelOrderResponse)
async def cancel_order_engine_order(
    idempotency_key: str,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> OrderEngineCancelOrderResponse:
    """§7.7's "an actual broker order already in flight" cancel path -- "a real `cancel`/`modify`
    API call, confirmed against broker state afterward rather than trusted from the API response
    alone." 404 means [idempotency_key] has no matching order at all (nothing to cancel -- e.g.
    it already filled, or was never placed), never conflated with a genuine cancel failure.
    """
    access_token = _load_access_token(token_store)
    order_service = OrderEngineOrderService(service)
    try:
        confirmed = await order_service.cancel_order(access_token, idempotency_key)
    except (UpstoxApiError, httpx.TimeoutException, httpx.TransportError) as exc:
        raise _upstox_call_error(exc) from exc

    if confirmed is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No matching order found to cancel")

    return OrderEngineCancelOrderResponse(status=confirmed.get("status"), raw=confirmed)


@router.put("/orders/{idempotency_key}/quantity", response_model=OrderEngineModifyQuantityResponse)
async def modify_order_engine_order_quantity(
    idempotency_key: str,
    body: OrderEngineModifyQuantityRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
) -> OrderEngineModifyQuantityResponse:
    """§7.7's quantity-only modify path -- "offered only on genuine `LIMIT` conditional entries
    ... the broker's native modify is called directly, result surfaced plainly." Every field
    besides quantity is carried over unchanged from the order's own current broker-reported state
    (see `OrderEngineOrderService.modify_order_quantity`); this route has no way to change price,
    order type, or trigger price, by design. 404 has the same "nothing to modify" meaning as
    [cancel_order_engine_order]'s own.
    """
    access_token = _load_access_token(token_store)
    order_service = OrderEngineOrderService(service)
    try:
        confirmed = await order_service.modify_order_quantity(
            access_token, idempotency_key, body.quantity,
        )
    except (UpstoxApiError, httpx.TimeoutException, httpx.TransportError) as exc:
        raise _upstox_call_error(exc) from exc

    if confirmed is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No matching order found to modify")

    return OrderEngineModifyQuantityResponse(status=confirmed.get("status"), raw=confirmed)


class OrderEngineExitPositionsRequest(BaseModel):
    """`None`/omitted [instrument_keys] closes every open position (mirrors the old
    `POST /orders/exit-all`); a non-empty list closes only those instruments -- e.g. "close only
    profitable positions", where the app itself decides which instrument_keys qualify (it already
    has live P&L from the WebSocket feed)."""

    instrument_keys: Optional[list[str]] = None


class OrderEngineExitPositionsResponse(BaseModel):
    status: Literal["success"] = "success"
    positions_found: int
    results: list[dict[str, Any]]


@router.post("/exit-positions", response_model=OrderEngineExitPositionsResponse)
async def exit_order_engine_positions(
    body: OrderEngineExitPositionsRequest,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> OrderEngineExitPositionsResponse:
    """Phase 0 of the GTT-to-order-engine cutover's own missing piece: the old model's
    `POST /orders/exit-all`/`POST /orders/exit-positions` (`smart_order_service.py`) had no
    order-engine equivalent -- this is it. Deliberately built on Upstox's own `GET /positions`
    (via [flatten_positions]) rather than `order_engine_ledger_store`'s `lots` table: a real held
    position is real held position regardless of which system opened it, and this is the one path
    that also gets the freeze-quantity slicing and retry-on-failure the ledger-driven
    `order_engine_max_loss_watcher.flatten_open_lots` doesn't have. Best-effort per position, same
    as the old route -- one instrument failing to flatten is reported in [results], not raised as
    an HTTP error.
    """
    access_token = _load_access_token(token_store)
    result = await flatten_positions(
        service, access_token,
        instrument_rules_service=InstrumentRulesService(settings),
        instrument_keys=body.instrument_keys,
    )
    return OrderEngineExitPositionsResponse(**result)


@router.post("/exit-all", response_model=OrderEngineExitPositionsResponse)
async def exit_all_order_engine_positions(
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    settings: Settings = Depends(get_settings),
) -> OrderEngineExitPositionsResponse:
    """Thin no-filter wrapper over [exit_order_engine_positions], same relationship the old
    `exit_all_positions`/`exit_positions` pair in `smart_order_service.py` had."""
    return await exit_order_engine_positions(
        OrderEngineExitPositionsRequest(instrument_keys=None),
        service=service, token_store=token_store, settings=settings,
    )


class OrderHistoryEntryResponse(BaseModel):
    """One `order_history` row -- see `docs/ORDER_HISTORY_V2_DESIGN.md` for the full column list
    and why each exists. Journaling columns are always `None` from this route today -- populated
    later by a future journal v2 UI/endpoint, not by anything in this backend yet."""

    id: str
    broker_order_id: str
    exchange_order_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    order_tag: Optional[str] = None
    instrument_key: str
    trading_symbol: Optional[str] = None
    transaction_type: str
    product: str
    order_type: str
    requested_quantity: int
    requested_price: Optional[float] = None
    trigger_price: Optional[float] = None
    status: str
    status_message: Optional[str] = None
    average_price: Optional[float] = None
    filled_quantity: int
    lot_id: Optional[str] = None
    rule_id: Optional[str] = None
    role: Optional[str] = None
    placed_at: Optional[str] = None
    last_broker_update_at: Optional[str] = None
    created_at: str
    updated_at: str
    strategy_tag: Optional[str] = None
    followed_plan: Optional[bool] = None
    mistake_reason: Optional[str] = None
    remarks: Optional[str] = None
    confidence_score: Optional[float] = None
    setup_type: Optional[str] = None


class OrderHistoryListResponse(BaseModel):
    orders: list[OrderHistoryEntryResponse]
    next_cursor: Optional[str] = None


@router.get("/orders", response_model=OrderHistoryListResponse)
async def list_order_history(
    limit: int = 50,
    before: Optional[str] = None,
    instrument_key: Optional[str] = None,
    order_status: Optional[str] = Query(default=None, alias="status"),
    lot_id: Optional[str] = None,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderHistoryListResponse:
    """Part 5's (`docs/ORDER_HISTORY_V2_DESIGN.md`) own paginated, server-owned order history --
    replaces the client's need to query broker order history directly (Upstox has no
    historical-orders endpoint at all). Cursor-based (`before` = a previous page's last row `id`),
    newest first, to avoid offset drift on this append-heavy, unbounded table.

    Unlike `GET /orders/{idempotency_key}` above (a synchronous, broker-live single-order lookup
    backing the placement-confirmation contract), this route never calls Upstox -- it only reads
    what `OrderHistoryRecorder` has already durably recorded, which may lag a live broker order by
    however long the reactive WS-push-then-re-fetch pipeline takes (typically near-instant, but
    never guaranteed synchronous)."""
    capped_limit = max(1, min(limit, 200))
    orders = ledger.list_orders(
        limit=capped_limit, before=before, instrument_key=instrument_key,
        status=order_status, lot_id=lot_id,
    )
    next_cursor = orders[-1]["id"] if len(orders) == capped_limit else None
    return OrderHistoryListResponse(
        orders=[OrderHistoryEntryResponse(**order) for order in orders],
        next_cursor=next_cursor,
    )


# -- §8's server-authoritative ledger -----------------------------------------------------------
#
# These routes are the ledger half of Part 4 (`docs/ORDER_POSITION_OVERHAUL_DESIGN.md` §8.1),
# distinct from everything above: the routes above place/cancel/modify a *real broker order*, these
# just record/read durable server-side state about a `Lot`/`TriggerRule`. The client-side
# `TriggerEvaluator` still makes the actual fire decision locally, fast, off its own tick stream;
# nothing here evaluates or fires anything, it only remembers what's happened.
#
# `PUT /ledger/lots` (the client-driven `Lot` upsert this section used to expose) was removed in
# Part 5 (`docs/ORDER_HISTORY_V2_DESIGN.md`): the server is now the sole writer of `lots`, deriving
# it itself from confirmed broker fills via `OrderHistoryRecorder` (see `app/main.py`'s
# `_record_order_history_from_push`), not from a client-computed mirror. `GET /ledger/lots/{id}`
# stays -- the read path is unaffected by who writes. Trigger-rule upsert routes below are
# untouched, still client-driven, out of Part 5's scope.
#
# Remaining upsert routes are simple upsert-by-id calls -- the client generates every id
# (`TriggerRule.id`), so a resend (retry after a dropped response) always lands on the same row
# rather than duplicating it, matching `OrderEngineLedgerStore.upsert_trigger_rule`'s own
# idempotent-by-construction behavior.


class OrderEngineLotResponse(BaseModel):
    lot: dict[str, Any]


class OrderEngineTriggerRuleUpsertRequest(BaseModel):
    rule_id: str = Field(min_length=1)
    lot_id: Optional[str] = None
    instrument_key: str = Field(min_length=1)
    role: Optional[str] = None
    state: str = Field(min_length=1)
    condition_op: Optional[str] = None
    condition_value: Optional[float] = None
    sibling_rule_id: Optional[str] = None


class OrderEngineTriggerRuleResponse(BaseModel):
    trigger_rule: dict[str, Any]


class OrderEngineTightenStopLossRequest(BaseModel):
    """A value-only update -- mirrors `TriggerDao.casUpdateConditionValue`'s own scope, since a
    trailing tighten never changes anything about a rule except its `condition_value`."""

    condition_value: float


class OrderEngineLotListResponse(BaseModel):
    lots: list[dict[str, Any]]


@router.get("/ledger/lots", response_model=OrderEngineLotListResponse)
async def list_ledger_lots(
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineLotListResponse:
    """`docs/ORDER_ENGINE_RELIABILITY_AUDIT.md`'s B5 phase 2: the "list my lots" endpoint the
    client's local Room `Lot` table currently exists only because there was no server-side
    equivalent -- every lot (open and closed, same "whole day matters" posture
    `get_ledger_pnl_summary`'s own `realized_pnl` sum already uses, oldest first) with its full
    row (`remaining_quantity`/`entry_price`/bracket prices/`state`/`product`/etc.), not the
    narrower `armed-brackets`/`pnl-summary` shapes, which each carry only the fields their own
    original caller needed."""
    return OrderEngineLotListResponse(lots=ledger.get_all_lots())


@router.get("/ledger/lots/{lot_id}", response_model=OrderEngineLotResponse)
async def get_ledger_lot(
    lot_id: str,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineLotResponse:
    lot = ledger.get_lot(lot_id)
    if lot is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No matching lot found")
    return OrderEngineLotResponse(lot=lot)


class OrderEngineModifyLotBracketRequest(BaseModel):
    """At least one of the two must be set -- a caller wanting to touch neither has nothing to
    call this route for."""

    target_price: Optional[float] = None
    stoploss_price: Optional[float] = None


class OrderEngineModifyLotBracketLegResult(BaseModel):
    outcome: Literal["modified", "no_such_leg", "not_armed"]


class OrderEngineModifyLotBracketResponse(BaseModel):
    lot: dict[str, Any]
    target: Optional[OrderEngineModifyLotBracketLegResult] = None
    stoploss: Optional[OrderEngineModifyLotBracketLegResult] = None


def _modify_bracket_leg(
    ledger: OrderEngineLedgerStore, rule_id: Optional[str], new_value: float,
) -> str:
    """One leg's own modify decision, same "a rule's own state decides what's legal" shape
    `cancel_ledger_trigger_rule`/the now-dead `TriggerRuleCanceller` both already use -- only a
    still-`ARMED` leg can move. Writes via [cas_update_trigger_rule_condition_value] rather than
    the old read-then-plain-upsert: a plain upsert echoed back whatever `state` this function
    read moments earlier, so a concurrent evaluator firing the same rule (ARMED -> FIRING ->
    PLACED, a real broker exit order now resting) between the read and the write got silently
    reverted back to ARMED here. The CAS only ever succeeds while the row is still ARMED at the
    exact version just read, so a lost race now correctly falls through to `not_armed` (re-read
    for a fresher, more honest error) instead of clobbering the evaluator's own transition.
    `no_such_leg` covers both "this lot never had this side armed" (`rule_id` is `None`) and "the
    id it recorded doesn't resolve to a real row" (shouldn't happen -- a real FK -- but never
    assumed)."""
    if not rule_id:
        return "no_such_leg"
    rule = ledger.get_trigger_rule(rule_id)
    if rule is None:
        return "no_such_leg"
    if rule.get("state") != "ARMED":
        return "not_armed"
    updated = ledger.cas_update_trigger_rule_condition_value(
        rule_id, expected_version=rule["version"], new_condition_value=new_value,
    )
    if updated is None:
        return "not_armed"
    return "modified"


@router.put("/ledger/lots/{lot_id}/bracket", response_model=OrderEngineModifyLotBracketResponse)
async def modify_ledger_lot_bracket(
    lot_id: str,
    body: OrderEngineModifyLotBracketRequest,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineModifyLotBracketResponse:
    """`docs/ORDER_ENGINE_RELIABILITY_AUDIT.md`'s B5 phase 2: the "modify an armed bracket leg"
    endpoint the client's `LotBracketModifier`/`NewEngineHomeScreen`'s "Modify" button need --
    that button is live today but silently no-ops against a real bracket (it only ever mutated a
    local `TriggerRule` row, never populated for a server-armed one post-Part-B3). Each side
    ([target_price]/[stoploss_price]) is resolved and modified independently via
    [_modify_bracket_leg] against [lot_id]'s own `target_rule_id`/`stoploss_rule_id` (the lots
    table's own denormalized pointers, set once by `order_engine_lot_bracket_armer.arm_lot_bracket`
    -- no separate trigger-rule scan needed); one leg's `not_armed`/`no_such_leg` outcome never
    blocks the other from succeeding. `lots.target_price`/`stoploss_price` themselves are updated
    to match only the leg(s) that actually succeeded, so a caller reading the returned [lot] sees
    exactly the durable prices now in effect, not the ones it merely asked for.
    """
    lot = ledger.get_lot(lot_id)
    if lot is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No matching lot found")
    if body.target_price is None and body.stoploss_price is None:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Must set at least one of target_price/stoploss_price",
        )

    target_result: Optional[OrderEngineModifyLotBracketLegResult] = None
    stoploss_result: Optional[OrderEngineModifyLotBracketLegResult] = None
    new_target_price: Optional[float] = None
    new_stoploss_price: Optional[float] = None

    if body.target_price is not None:
        outcome = _modify_bracket_leg(ledger, lot.get("target_rule_id"), body.target_price)
        target_result = OrderEngineModifyLotBracketLegResult(outcome=outcome)
        if outcome == "modified":
            new_target_price = body.target_price
            ledger.record_event(
                event_type="TRIGGER_RULE_MODIFIED", lot_id=lot_id, rule_id=lot.get("target_rule_id"),
                payload={"condition_value": body.target_price},
            )

    if body.stoploss_price is not None:
        outcome = _modify_bracket_leg(ledger, lot.get("stoploss_rule_id"), body.stoploss_price)
        stoploss_result = OrderEngineModifyLotBracketLegResult(outcome=outcome)
        if outcome == "modified":
            new_stoploss_price = body.stoploss_price
            ledger.record_event(
                event_type="TRIGGER_RULE_MODIFIED", lot_id=lot_id, rule_id=lot.get("stoploss_rule_id"),
                payload={"condition_value": body.stoploss_price},
            )

    updated_lot = lot
    if new_target_price is not None or new_stoploss_price is not None:
        updated_lot = ledger.update_lot_bracket_price(
            lot_id, target_price=new_target_price, stoploss_price=new_stoploss_price,
        ) or lot

    return OrderEngineModifyLotBracketResponse(lot=updated_lot, target=target_result, stoploss=stoploss_result)


@router.put(
    "/ledger/trigger-rules", response_model=OrderEngineTriggerRuleResponse,
)
async def upsert_ledger_trigger_rule(
    body: OrderEngineTriggerRuleUpsertRequest,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineTriggerRuleResponse:
    """Records/updates one `TriggerRule` durably server-side -- called after every local
    `TriggerRepository` state transition (arm, fire, cancel, OCO-cancel) per §8.1. The server
    never evaluates this rule itself; it only remembers what the client's own `TriggerEvaluator`
    already decided."""
    rule = ledger.upsert_trigger_rule(
        rule_id=body.rule_id,
        lot_id=body.lot_id,
        instrument_key=body.instrument_key,
        role=body.role,
        state=body.state,
        condition_op=body.condition_op,
        condition_value=body.condition_value,
        sibling_rule_id=body.sibling_rule_id,
    )
    ledger.record_event(
        event_type="TRIGGER_RULE_UPSERTED", lot_id=body.lot_id, rule_id=body.rule_id,
        payload={"state": body.state},
    )
    return OrderEngineTriggerRuleResponse(trigger_rule=rule)


@router.put(
    "/ledger/trigger-rules/{rule_id}/tighten-stop-loss",
    response_model=OrderEngineTriggerRuleResponse,
)
async def tighten_ledger_trigger_rule_stop_loss(
    rule_id: str,
    body: OrderEngineTightenStopLossRequest,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineTriggerRuleResponse:
    """Mirrors `TriggerRepository.tightenStopLoss`'s value-only CAS -- the rule stays `ARMED`
    throughout, only `condition_value` moves. 404 if the client references a rule the server has
    never seen (e.g. this call raced ahead of the rule's own initial upsert), 409 if the rule is
    no longer `ARMED` (already fired/cancelled, whether from this same stale read or a genuinely
    concurrent one) -- via [cas_update_trigger_rule_condition_value], not the old plain
    read-then-upsert, which echoed back a possibly-stale `state` and could revert a rule the
    evaluator had just fired back to `ARMED` underneath it."""
    existing = ledger.get_trigger_rule(rule_id)
    if existing is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No matching trigger rule found")
    if existing.get("state") != "ARMED":
        raise _http_error(status.HTTP_409_CONFLICT, "Trigger rule is no longer armed")

    rule = ledger.cas_update_trigger_rule_condition_value(
        rule_id, expected_version=existing["version"], new_condition_value=body.condition_value,
    )
    if rule is None:
        raise _http_error(status.HTTP_409_CONFLICT, "Trigger rule is no longer armed")
    ledger.record_event(
        event_type="TRIGGER_RULE_TIGHTENED", lot_id=existing["lot_id"], rule_id=rule_id,
        payload={"condition_value": body.condition_value},
    )
    return OrderEngineTriggerRuleResponse(trigger_rule=rule)


class OrderEngineCancelTriggerRuleResponse(BaseModel):
    """Server-side twin of Android's own (now-dead, post-Part-B3) `TriggerRuleCanceller` outcome
    shape (`TriggerCancelOutcome`) -- one flat response instead of a sealed class, since this is
    the wire boundary, not the domain type itself."""

    outcome: Literal[
        "cancelled_internally", "cancelled_on_broker", "not_found_on_broker", "rejected",
    ]
    trigger_rule: Optional[dict[str, Any]] = None
    broker_status: Optional[str] = None
    reason: Optional[str] = None


@router.post(
    "/ledger/trigger-rules/{rule_id}/cancel",
    response_model=OrderEngineCancelTriggerRuleResponse,
)
async def cancel_ledger_trigger_rule(
    rule_id: str,
    service: UpstoxService = Depends(get_upstox_service),
    token_store: EncryptedTokenStore = Depends(get_token_store),
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineCancelTriggerRuleResponse:
    """Closes the gap `docs/ORDER_ENGINE_RELIABILITY_AUDIT.md` names as "no real way to cancel a
    specific server-armed bracket leg from the client" -- since Part B3, bracket legs are armed and
    fired entirely server-side, so the client's own local `TriggerRuleCanceller` (which only ever
    read a local Room `TriggerRule` row) can never find anything to act on. This route is that
    class's logic, ported server-side, working against the real, authoritative `trigger_rules` row:

    - `ARMED` -> a pure internal CAS (`cas_update_trigger_rule_state`, race-safe against the
      evaluator's own tick-driven CAS to `FIRING` -- a rule that wins that race between this route
      reading it and applying the transition is rejected with 409, never silently dropped).
    - `PLACED` -> a real broker cancel via `OrderEngineOrderService.cancel_order`, keyed by
      [rule_id] itself (the same idempotency key `order_engine_trigger_evaluator._fire_rule` placed
      the exit order with), confirmed against broker state per that method's own discipline, then
      the local row is CAS'd `PLACED -> CANCELLED` to match (re-reading the row's current version
      first, since the evaluator could have moved it in the time the broker call took).
    - Every other state (`EVALUATING`, `FIRING`, `FAILED`, `CANCELLED`) has nothing legitimate to
      cancel -- rejected with a reason, same "never silently dropped or falsely reported as
      successful" discipline `TriggerRuleCanceller`'s own doc comment states.

    404 if [rule_id] doesn't exist in the ledger at all.
    """
    rule = ledger.get_trigger_rule(rule_id)
    if rule is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No matching trigger rule found")

    state = rule.get("state")

    if state == "ARMED":
        cancelled = ledger.cas_update_trigger_rule_state(rule_id, rule["version"], "CANCELLED")
        if cancelled is None:
            raise _http_error(
                status.HTTP_409_CONFLICT,
                "Trigger rule changed state before this cancel could apply -- refresh and retry.",
            )
        ledger.record_event(
            event_type="TRIGGER_RULE_CANCELLED", lot_id=cancelled.get("lot_id"), rule_id=rule_id,
            payload={"via": "internal_cas"},
        )
        return OrderEngineCancelTriggerRuleResponse(
            outcome="cancelled_internally", trigger_rule=cancelled,
        )

    if state == "PLACED":
        access_token = _load_access_token(token_store)
        order_service = OrderEngineOrderService(service)
        try:
            confirmed = await order_service.cancel_order(access_token, rule_id)
        except (UpstoxApiError, httpx.TimeoutException, httpx.TransportError) as exc:
            raise _upstox_call_error(exc) from exc

        if confirmed is None:
            return OrderEngineCancelTriggerRuleResponse(outcome="not_found_on_broker")

        # Re-read the row's own *current* state/version rather than trusting the stale [rule] this
        # handler already holds -- the evaluator's own reconciliation could have moved it while the
        # broker call above was in flight. Only CAS if it's still genuinely PLACED.
        current = ledger.get_trigger_rule(rule_id) or rule
        cancelled = None
        if current.get("state") == "PLACED":
            cancelled = ledger.cas_update_trigger_rule_state(rule_id, current["version"], "CANCELLED")
            if cancelled is not None:
                ledger.record_event(
                    event_type="TRIGGER_RULE_CANCELLED", lot_id=cancelled.get("lot_id"),
                    rule_id=rule_id, payload={"via": "broker_cancel"},
                )
        return OrderEngineCancelTriggerRuleResponse(
            outcome="cancelled_on_broker",
            trigger_rule=cancelled if cancelled is not None else current,
            broker_status=confirmed.get("status"),
        )

    return OrderEngineCancelTriggerRuleResponse(
        outcome="rejected", reason=f"cannot cancel a rule in state {state}",
    )


class OrderEngineMaxLossEpochUpsertRequest(BaseModel):
    """§7.10's 2026-08-12 amendment: the user chooses, client-side, whether `threshold_x` is a
    fixed absolute amount or a percentage of the breach formula's own reference point -- this
    request carries that choice to the server so `order_engine_max_loss_watcher.py` (the primary,
    server-side enforcer per §8.3) honors it, not just the client's own local
    `MaxLossAggregator` backstop."""

    opening_balance: float
    peak_equity: float
    threshold_x: float = Field(gt=0)
    threshold_mode: Literal["ABSOLUTE", "PERCENTAGE"] = "ABSOLUTE"
    epoch_started_at: str


class OrderEngineMaxLossEpochResponse(BaseModel):
    epoch: dict[str, Any]


@router.put("/ledger/max-loss-epoch", response_model=OrderEngineMaxLossEpochResponse)
async def upsert_ledger_max_loss_epoch(
    body: OrderEngineMaxLossEpochUpsertRequest,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineMaxLossEpochResponse:
    """Mirrors the client's own `MaxLossEpochRepository.startEpoch` -- called whenever the user
    (re)starts a max-loss epoch from `NewEngineHomeScreen`'s form, so the server-side watcher
    enforces the exact same opening balance/threshold/mode the client just armed locally, not a
    stale or default one. Single-row, same upsert-wholesale semantics as every other ledger write
    here."""
    epoch = ledger.upsert_max_loss_epoch(
        opening_balance=body.opening_balance,
        peak_equity=body.peak_equity,
        threshold_x=body.threshold_x,
        threshold_mode=body.threshold_mode,
        epoch_started_at=body.epoch_started_at,
    )
    ledger.record_event(
        event_type="MAX_LOSS_EPOCH_UPSERTED",
        payload={"threshold_x": body.threshold_x, "threshold_mode": body.threshold_mode},
    )
    return OrderEngineMaxLossEpochResponse(epoch=epoch)


@router.get("/ledger/max-loss-epoch", response_model=OrderEngineMaxLossEpochResponse)
async def get_ledger_max_loss_epoch(
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineMaxLossEpochResponse:
    """Lets the client stay synced with what the server-side watcher actually has armed --
    without this, `PUT` was write-only and a client (a fresh install, a second device, or just a
    screen re-opened after the app was killed) had no way to confirm the epoch it's displaying
    still matches what `order_engine_max_loss_watcher.py` is really enforcing. 404 if no epoch has
    ever been started -- never a fabricated default, same "confirmed absent, not guessed" posture
    every other lookup route in this file already uses."""
    epoch = ledger.get_max_loss_epoch()
    if epoch is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No max-loss epoch has been started")
    return OrderEngineMaxLossEpochResponse(epoch=epoch)


class OrderEngineLotPnlResponse(BaseModel):
    """One open lot's own live P&L, added 2026-08-13 as the per-lot breakdown
    [OrderEnginePnlSummaryResponse] never had -- see that model's own doc comment for why the gap
    mattered (the home screen's per-lot row silently showed a permanent `0.0` while a lot stayed
    open, only ever the whole-portfolio total was ever live)."""

    lot_id: str
    instrument_key: str
    ltp: float
    unrealized_pnl: float


class OrderEnginePnlSummaryResponse(BaseModel):
    """§8.2's live-PnL formula, summarized -- the REST counterpart to the WS-pushed
    `dispatch_order_engine_lot_status` (per-lot, not a total). Built for a feed-less screen (the
    new engine's own home screen has no `BackendFeedClient` by design) that still wants a genuine
    live open-position number rather than nothing, or a stale local-only one.

    [per_lot] added 2026-08-13, same session as [OrderEngineLotPnlResponse] -- found live, the
    home screen never had anywhere to source a per-lot live number from at all, so every open
    lot's row rendered its permanently-zero `realizedPnl` instead."""

    realized_pnl: float
    unrealized_pnl: float
    open_lot_count: int
    per_lot: list[OrderEngineLotPnlResponse] = Field(default_factory=list)


@router.get("/ledger/pnl-summary", response_model=OrderEnginePnlSummaryResponse)
async def get_ledger_pnl_summary(
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
    lot_tracker: OrderEngineLotTracker = Depends(get_order_engine_lot_tracker),
) -> OrderEnginePnlSummaryResponse:
    """`realized_pnl` sums every lot's own banked `realized_pnl` (open or closed, same whole-day
    figure the client's own `PnLCalculator.realizedPnl` sums); `unrealized_pnl` is
    [OrderEngineLotTracker.total_live_pnl] -- the *same* app-lifetime tracker instance the live
    tick path/`order_engine_max_loss_watcher.py` already use (see
    `get_order_engine_lot_tracker`'s own doc comment for why this must be the singleton, not a
    fresh instance), so this number is only ever as stale as the tracker's own last-seen-tick
    cache, never fabricated from a fresh, empty one. `per_lot` is the same tracker's
    [OrderEngineLotTracker.per_lot_live_pnl], one row per currently-open lot -- both numbers derive
    from the identical in-memory `_last_ltp` cache, so the sum of `per_lot`'s own `unrealized_pnl`
    values always matches this response's own `unrealized_pnl` field exactly, never a separately-
    computed approximation of it. No auth/scope beyond the router's own `require_mobile_api_key`
    -- there's nothing lot-specific-secret or mutating here."""
    total_realized = sum(_pnl_number(lot.get("realized_pnl")) for lot in ledger.get_all_lots())
    return OrderEnginePnlSummaryResponse(
        realized_pnl=total_realized,
        unrealized_pnl=lot_tracker.total_live_pnl(),
        open_lot_count=len(ledger.get_open_lots()),
        per_lot=[
            OrderEngineLotPnlResponse(
                lot_id=status.lot_id,
                instrument_key=status.instrument_key,
                ltp=status.ltp,
                unrealized_pnl=status.live_pnl,
            )
            for status in lot_tracker.per_lot_live_pnl()
        ],
    )


@router.post("/ledger/admin/reset", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def reset_ledger_and_journal(
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
    journal: JournalStore = Depends(get_journal_store),
) -> None:
    """Settings-page "clear server DB" button (2026-08-18) -- the app-reachable version of the
    manual `DELETE FROM` + `VACUUM` wipe run by hand on the VPS on 2026-08-18 and again on
    2026-08-18 (a stuck server-side "trade still running" lot after an outside-the-app exit,
    expected to recur "multiple times until we fix the live market issues", per the user's own
    framing). Clears both [OrderEngineLedgerStore.reset_all] (lots/trigger_rules/
    order_engine_events) and [JournalStore.reset_all] (trade history) -- same full-reset scope the
    manual wipe used, not a partial one. No auth beyond the router's own `require_mobile_api_key`;
    this is a destructive, irreversible action and the client is expected to confirm with the user
    before ever calling it."""
    ledger.reset_all()
    journal.reset_all()


class OrderEngineHealthResponse(BaseModel):
    """§6.4 Part B4's heartbeat exposure -- what the Android client's `EngineHeartbeatMonitor`
    polls to tell whether the server's own trigger-evaluation loop is actually advancing, not just
    whether this HTTP endpoint itself is reachable. `last_evaluated_at` is `None` before this
    backend process has evaluated anything at all (a fresh restart, or before the first tick/
    fallback-loop pass) -- the client's own `EngineHeartbeatMonitor.observe` needs a real `Instant`,
    so a caller should treat `None` here as "not enough data yet," not as an immediate DEGRADED
    verdict."""

    last_evaluated_at: Optional[str] = None


@router.get("/engine-health", response_model=OrderEngineHealthResponse)
async def get_order_engine_health() -> OrderEngineHealthResponse:
    stamp = order_engine_trigger_evaluator.last_evaluated_at()
    return OrderEngineHealthResponse(last_evaluated_at=stamp.isoformat() if stamp is not None else None)


class OrderEngineArmedBracketResponse(BaseModel):
    """One `ARMED` bracket leg, with its own lot's exit-order shape already resolved -- see
    `OrderEngineLedgerStore.get_armed_brackets_with_lot_info`'s own doc comment for why this is a
    join, not a separate per-rule lot fetch. [rule_id] is deliberately reused as the fallback
    order's own idempotency key by the Android client -- the *same* id this server would use if it
    fired this exact rule itself, so a late-recovering server's own attempt is rejected as a
    duplicate rather than doubling the exit."""

    rule_id: str
    instrument_key: str
    role: Optional[str] = None
    condition_op: Optional[str] = None
    condition_value: Optional[float] = None
    lot_id: Optional[str] = None
    lot_transaction_type: Optional[str] = None
    lot_remaining_quantity: Optional[int] = None
    lot_product: Optional[str] = None


class OrderEngineArmedBracketsResponse(BaseModel):
    brackets: list[OrderEngineArmedBracketResponse]


@router.get("/ledger/armed-brackets", response_model=OrderEngineArmedBracketsResponse)
async def get_ledger_armed_brackets(
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineArmedBracketsResponse:
    """§6.4 Part B4: what `ClientFallbackEvaluator` arms itself from once
    `EngineHeartbeatMonitor` reports `DEGRADED` -- every bracket leg the server itself currently
    considers armed and would fire, in the exact shape the client needs to build its own
    equivalent `ClientFallbackRule` without a second round trip per rule."""
    return OrderEngineArmedBracketsResponse(
        brackets=[
            OrderEngineArmedBracketResponse(**row)
            for row in ledger.get_armed_brackets_with_lot_info()
        ],
    )


class OrderEnginePendingAmbiguousFillResponse(BaseModel):
    """One unresolved external-order-detection ambiguity -- see
    `OrderEngineLedgerStore`'s `pending_ambiguous_fills` table doc comment. Surfaced in the
    Positions screen; the user picks which of [candidate_lot_ids] the fill actually closed, or
    leaves it (no dismiss/ignore action exists -- it simply stays pending until resolved)."""

    id: str
    order_id: str
    instrument_key: str
    candidate_lot_ids: list[str]
    transaction_type: Optional[str] = None
    average_price: Optional[float] = None
    filled_quantity: Optional[int] = None
    created_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "OrderEnginePendingAmbiguousFillResponse":
        broker_order = row["broker_order"]
        return cls(
            id=row["id"],
            order_id=row["order_id"],
            instrument_key=row["instrument_key"],
            candidate_lot_ids=row["candidate_lot_ids"],
            transaction_type=broker_order.get("transaction_type"),
            average_price=broker_order.get("average_price"),
            filled_quantity=broker_order.get("filled_quantity"),
            created_at=row["created_at"],
        )


class OrderEnginePendingAmbiguousFillsResponse(BaseModel):
    fills: list[OrderEnginePendingAmbiguousFillResponse]


@router.get("/ledger/pending-ambiguous-fills", response_model=OrderEnginePendingAmbiguousFillsResponse)
async def get_pending_ambiguous_fills(
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEnginePendingAmbiguousFillsResponse:
    return OrderEnginePendingAmbiguousFillsResponse(
        fills=[
            OrderEnginePendingAmbiguousFillResponse.from_row(row)
            for row in ledger.get_pending_ambiguous_fills()
        ],
    )


class OrderEngineResolvePendingAmbiguousFillRequest(BaseModel):
    lot_id: str


class OrderEngineResolvePendingAmbiguousFillResponse(BaseModel):
    resolved: bool
    lot_id: Optional[str] = None


@router.post(
    "/ledger/pending-ambiguous-fills/{pending_id}/resolve",
    response_model=OrderEngineResolvePendingAmbiguousFillResponse,
)
async def resolve_pending_ambiguous_fill(
    pending_id: str,
    body: OrderEngineResolvePendingAmbiguousFillRequest,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineResolvePendingAmbiguousFillResponse:
    """The only action path for an ambiguous external exit (§ the `pending_ambiguous_fills` table
    doc comment) -- the push notification fired alongside the original detection is purely
    informational, so there is nothing else that could race with this."""
    pending = ledger.get_pending_ambiguous_fill(pending_id)
    if pending is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No such pending ambiguous fill")
    if body.lot_id not in pending["candidate_lot_ids"]:
        raise _http_error(status.HTTP_400_BAD_REQUEST, "lot_id is not one of this fill's candidates")

    lot = ledger.get_lot(body.lot_id)
    entry_transaction_type = lot.get("transaction_type") if lot else None
    recorder = OrderHistoryRecorder(ledger)
    mutated_lot = recorder.apply_fill_to_ledger(
        pending["broker_order"], lot_id=body.lot_id, role="EXIT",
        entry_transaction_type=entry_transaction_type,
    )
    recorder.record_order_snapshot(
        pending["broker_order"], lot_id=body.lot_id, role="EXIT",
    )
    ledger.delete_pending_ambiguous_fill(pending_id)
    return OrderEngineResolvePendingAmbiguousFillResponse(
        resolved=mutated_lot is not None, lot_id=body.lot_id,
    )


def _pnl_number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

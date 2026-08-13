from __future__ import annotations

import logging
from typing import Any, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.dependencies import (
    get_order_engine_ledger_store,
    get_order_engine_lot_tracker,
    get_token_store,
    get_upstox_service,
)
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.core.security import require_mobile_api_key
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker
from app.services.order_engine_order_service import OrderEngineOrderService, UnintendedShortGuardError
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


# -- §8's server-authoritative ledger -----------------------------------------------------------
#
# These four routes are the ledger half of Part 4 (`docs/ORDER_POSITION_OVERHAUL_DESIGN.md` §8.1),
# distinct from everything above: the routes above place/cancel/modify a *real broker order*, these
# four just record durable server-side state about a `Lot`/`TriggerRule` the client already has.
# The server becomes authoritative for this record (§8.1's own framing: the client's local Room DB
# becomes a synced cache, not the primary record) -- but the client-side `TriggerEvaluator` still
# makes the actual fire decision locally, fast, off its own tick stream; nothing here evaluates or
# fires anything, it only remembers what the client has already decided.
#
# All four are simple upsert-by-id calls -- the client generates every id (`Lot.id`/
# `TriggerRule.id`), so a resend (retry after a dropped response) always lands on the same row
# rather than duplicating it, matching `OrderEngineLedgerStore.upsert_lot`/`upsert_trigger_rule`'s
# own idempotent-by-construction behavior.


class OrderEngineLotUpsertRequest(BaseModel):
    """Mirrors the Android `Lot`/`LotBracket` shape closely enough for a durable server-side
    record -- not a 1:1 field copy, just what §8's exit-reconciliation/live-PnL/max-loss watcher
    machinery actually needs to know."""

    lot_id: str = Field(min_length=1)
    instrument_key: str = Field(min_length=1)
    transaction_type: Literal["BUY", "SELL"]
    entry_price: Optional[float] = None
    entry_quantity: int = Field(gt=0)
    remaining_quantity: int = Field(ge=0)
    realized_pnl: float = 0.0
    state: str = Field(min_length=1)
    target_price: Optional[float] = None
    stoploss_price: Optional[float] = None
    trailing_gap: Optional[float] = None
    target_rule_id: Optional[str] = None
    stoploss_rule_id: Optional[str] = None
    product: str = "I"


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


@router.put("/ledger/lots", response_model=OrderEngineLotResponse)
async def upsert_ledger_lot(
    body: OrderEngineLotUpsertRequest,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineLotResponse:
    """Records/updates one `Lot` durably server-side -- called after every local
    `LotRepository` mutation (create, exit, transition) per §8.1. Idempotent by `lot_id`."""
    lot = ledger.upsert_lot(
        lot_id=body.lot_id,
        instrument_key=body.instrument_key,
        transaction_type=body.transaction_type,
        entry_price=body.entry_price,
        entry_quantity=body.entry_quantity,
        remaining_quantity=body.remaining_quantity,
        realized_pnl=body.realized_pnl,
        state=body.state,
        target_price=body.target_price,
        stoploss_price=body.stoploss_price,
        trailing_gap=body.trailing_gap,
        target_rule_id=body.target_rule_id,
        stoploss_rule_id=body.stoploss_rule_id,
        product=body.product,
    )
    ledger.record_event(event_type="LOT_UPSERTED", lot_id=body.lot_id, payload={"state": body.state})
    return OrderEngineLotResponse(lot=lot)


@router.get("/ledger/lots/{lot_id}", response_model=OrderEngineLotResponse)
async def get_ledger_lot(
    lot_id: str,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> OrderEngineLotResponse:
    lot = ledger.get_lot(lot_id)
    if lot is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No matching lot found")
    return OrderEngineLotResponse(lot=lot)


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
    never seen (e.g. this call raced ahead of the rule's own initial upsert)."""
    existing = ledger.get_trigger_rule(rule_id)
    if existing is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "No matching trigger rule found")

    rule = ledger.upsert_trigger_rule(
        rule_id=rule_id,
        lot_id=existing["lot_id"],
        instrument_key=existing["instrument_key"],
        role=existing["role"],
        state=existing["state"],
        condition_op=existing["condition_op"],
        condition_value=body.condition_value,
        sibling_rule_id=existing["sibling_rule_id"],
    )
    ledger.record_event(
        event_type="TRIGGER_RULE_TIGHTENED", lot_id=existing["lot_id"], rule_id=rule_id,
        payload={"condition_value": body.condition_value},
    )
    return OrderEngineTriggerRuleResponse(trigger_rule=rule)


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


def _pnl_number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

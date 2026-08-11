from __future__ import annotations

import logging
from typing import Any, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.dependencies import get_token_store, get_upstox_service
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.core.security import require_mobile_api_key
from app.services.order_engine_order_service import OrderEngineOrderService
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
        )
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

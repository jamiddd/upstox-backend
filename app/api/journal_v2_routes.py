from __future__ import annotations

import logging
import uuid
from typing import Any, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.dependencies import get_order_engine_ledger_store
from app.core.security import require_mobile_api_key, require_mobile_or_web
from app.services.order_engine_ledger_store import OrderEngineLedgerStore

logger = logging.getLogger(__name__)

# Journal/Analytics v2 (design session 2026-08-16, `~/.claude/plans/eager-juggling-ripple.md`).
# A fresh, parallel pipeline reading off `order_history`/`lots` -- the new order engine's own
# server-authoritative record of every fill (Part 5, `docs/ORDER_HISTORY_V2_DESIGN.md`) -- instead
# of v1 journal's (`journal_store.py`) independent fill-ingestion + FIFO-ish matcher. v1 stays
# running untouched; this is new code only, same isolation posture as every other phase of the
# order/position overhaul. A "trade" here is a closed `lots` row; manual entries and unmatched
# real orders are `order_history` rows with `role='MANUAL'`. Read routes (`GET`) are on
# `dual_router` (require_mobile_or_web) matching v1's own web-exposure convention; write routes
# (notes, manual trade creation) are mobile-only for now, same as v1's write surface.

protected_router = APIRouter(dependencies=[Depends(require_mobile_api_key)])
dual_router = APIRouter(dependencies=[Depends(require_mobile_or_web)])


class JournalV2TradeResponse(BaseModel):
    """One closed `lots` row, shaped as a "trade" for the journal list/detail screens."""

    id: str
    instrument_key: str
    trading_symbol: Optional[str] = None
    transaction_type: str
    entry_price: Optional[float] = None
    entry_quantity: int
    remaining_quantity: int
    realized_pnl: float
    state: str
    product: str
    created_at: str
    updated_at: str
    strategy_tag: Optional[str] = None
    followed_plan: Optional[bool] = None
    mistake_reason: Optional[str] = None
    remarks: Optional[str] = None
    confidence_score: Optional[float] = None
    setup_type: Optional[str] = None


class JournalV2TradeListResponse(BaseModel):
    trades: list[JournalV2TradeResponse]
    next_cursor: Optional[str] = None


class JournalV2OrderResponse(BaseModel):
    """One constituent `order_history` row for a trade's detail view -- same shape as
    `OrderHistoryEntryResponse` in `order_engine_routes.py`, kept as a separate model here since
    the two routers evolve independently."""

    id: str
    broker_order_id: str
    instrument_key: str
    trading_symbol: Optional[str] = None
    transaction_type: str
    product: str
    order_type: str
    requested_quantity: int
    status: str
    average_price: Optional[float] = None
    filled_quantity: int
    role: Optional[str] = None
    placed_at: Optional[str] = None
    created_at: str
    charges: Optional[float] = None
    strategy_tag: Optional[str] = None
    followed_plan: Optional[bool] = None
    mistake_reason: Optional[str] = None
    remarks: Optional[str] = None
    confidence_score: Optional[float] = None
    setup_type: Optional[str] = None


class JournalV2TradeDetailResponse(BaseModel):
    trade: JournalV2TradeResponse
    orders: list[JournalV2OrderResponse]


class JournalV2NotesRequest(BaseModel):
    strategy_tag: Optional[str] = None
    followed_plan: Optional[bool] = None
    mistake_reason: Optional[str] = None
    remarks: Optional[str] = None
    confidence_score: Optional[float] = None
    setup_type: Optional[str] = None


class JournalV2ManualTradeRequest(BaseModel):
    instrument_key: str
    trading_symbol: Optional[str] = None
    transaction_type: str
    product: str = "I"
    quantity: int
    average_price: Optional[float] = None
    placed_at: Optional[str] = None


class JournalV2FilterOptionsResponse(BaseModel):
    trading_symbols: list[str]
    setups: list[str]
    strategy_tags: list[str]


class AnalyticsV2WeekdayEntry(BaseModel):
    label: str
    trade_count: int
    net_pnl: float
    win_rate: float
    low_sample: bool


class AnalyticsV2SummaryResponse(BaseModel):
    """Same shape as v1's `/analytics/summary` (`journal_store.analytics_summary`) with one
    deliberate difference: `net_pnl` is always `None` here -- charges aren't computed at
    order/day granularity yet (see `OrderEngineLedgerStore.journal_analytics_summary`'s own
    docstring). Every other figure is gross P&L."""

    trade_count: int
    gross_pnl: float
    net_pnl: Optional[float] = None
    win_rate: float
    average_win: float
    average_loss: float
    best_trade: float
    worst_trade: float
    equity_curve: list[float]
    low_sample: bool
    weekday_breakdown: list[AnalyticsV2WeekdayEntry]


def _trade_response(lot: dict[str, Any]) -> JournalV2TradeResponse:
    payload = dict(lot)
    if payload.get("followed_plan") is not None:
        payload["followed_plan"] = bool(payload["followed_plan"])
    return JournalV2TradeResponse(**payload)


@dual_router.get("/journal/v2/trades", response_model=JournalV2TradeListResponse)
async def list_journal_v2_trades(
    limit: int = 50,
    before: Optional[str] = None,
    instrument_key: Optional[str] = None,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> JournalV2TradeListResponse:
    capped_limit = max(1, min(limit, 200))
    lots = ledger.list_closed_lots(limit=capped_limit, before=before, instrument_key=instrument_key)
    next_cursor = lots[-1]["id"] if len(lots) == capped_limit else None
    return JournalV2TradeListResponse(
        trades=[_trade_response(lot) for lot in lots], next_cursor=next_cursor,
    )


@dual_router.get("/journal/v2/trades/{trade_id}", response_model=JournalV2TradeDetailResponse)
async def get_journal_v2_trade(
    trade_id: str,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> JournalV2TradeDetailResponse:
    lot = ledger.get_lot(trade_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    orders = ledger.get_orders_for_lot(trade_id)
    # `lots` has no trading_symbol column of its own (see `list_closed_lots`'s own docstring) --
    # for the detail view, pull it off the constituent orders already being fetched here anyway,
    # rather than a second query.
    trading_symbol = next(
        (order["trading_symbol"] for order in orders if order.get("trading_symbol")), None,
    )
    return JournalV2TradeDetailResponse(
        trade=_trade_response({**lot, "trading_symbol": trading_symbol}),
        orders=[JournalV2OrderResponse(**order) for order in orders],
    )


@dual_router.patch("/journal/v2/trades/{trade_id}/notes")
async def update_journal_v2_trade_notes(
    trade_id: str,
    body: JournalV2NotesRequest,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> Union[JournalV2TradeResponse, JournalV2OrderResponse]:
    """`trade_id` may be a `lots.id` (the normal case) or an `order_history.id` for a
    `role='MANUAL'` row that has no lot -- tries the lot table first, falls back to
    `order_history`, matching how `create_manual_order` rows are surfaced everywhere else."""
    fields = body.model_dump(exclude_unset=True)
    if "followed_plan" in fields and fields["followed_plan"] is not None:
        fields["followed_plan"] = int(fields["followed_plan"])
    lot = ledger.get_lot(trade_id)
    if lot is not None:
        updated = ledger.update_lot_notes(trade_id, **fields)
        return _trade_response(updated)  # type: ignore[arg-type]
    order = ledger.update_order_notes(trade_id, **fields)
    if order is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return JournalV2OrderResponse(**order)


@dual_router.get("/journal/v2/filter-options", response_model=JournalV2FilterOptionsResponse)
async def get_journal_v2_filter_options(
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> JournalV2FilterOptionsResponse:
    return JournalV2FilterOptionsResponse(**ledger.journal_filter_options())


@protected_router.post("/journal/v2/trades", response_model=JournalV2OrderResponse)
async def create_journal_v2_manual_trade(
    body: JournalV2ManualTradeRequest,
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> JournalV2OrderResponse:
    order = ledger.create_manual_order(
        order_id=str(uuid.uuid4()),
        instrument_key=body.instrument_key,
        trading_symbol=body.trading_symbol,
        transaction_type=body.transaction_type,
        product=body.product,
        quantity=body.quantity,
        average_price=body.average_price,
        placed_at=body.placed_at,
    )
    return JournalV2OrderResponse(**order)


@dual_router.get("/analytics/v2/summary", response_model=AnalyticsV2SummaryResponse)
async def get_analytics_v2_summary(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    ledger: OrderEngineLedgerStore = Depends(get_order_engine_ledger_store),
) -> AnalyticsV2SummaryResponse:
    return AnalyticsV2SummaryResponse(
        **ledger.journal_analytics_summary(start_date=start_date, end_date=end_date),
    )


router = APIRouter()
router.include_router(protected_router)
router.include_router(dual_router)

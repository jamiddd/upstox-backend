from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_bracket_armer import arm_lot_bracket
from app.services.realized_pnl import compute_realized_pnl

"""Part 5 (`docs/ORDER_HISTORY_V2_DESIGN.md`) -- the backend's own record of every real broker
order the new order engine ever places, and the place `lots` derivation now genuinely happens
server-side instead of via the client's `PUT /order-engine/ledger/lots` mirror.

This is called only with an already-confirmed broker order-book row (a fresh re-fetch, never the
raw portfolio-feed WS push payload directly) -- same "broker ground truth, always re-confirmed,
never taken on faith" discipline `ExitReconciliationChecker` already established. Business/
derivation logic lives here, not in `OrderEngineLedgerStore` itself, which stays a plain
persistence layer per its own "record, not evaluator" framing.
"""


class OrderHistoryRecorder:
    def __init__(self, ledger_store: OrderEngineLedgerStore) -> None:
        self._ledger_store = ledger_store

    def record_placement(
        self,
        *,
        broker_order_id: str,
        order_tag: Optional[str],
        idempotency_key: str,
        role: Literal["ENTRY", "EXIT", "MANUAL"],
        instrument_key: str,
        transaction_type: str,
        product: str,
        order_type: str,
        requested_quantity: int,
        requested_price: Optional[float],
        trigger_price: Optional[float],
        target_price: Optional[float] = None,
        stoploss_price: Optional[float] = None,
        trailing_gap: Optional[float] = None,
    ) -> dict[str, Any]:
        """The one deliberate exception to "only ever write from a confirmed broker re-fetch":
        called synchronously right after a successful placement, using only data the caller
        itself supplied (the request it just sent) plus the `order_id` Upstox's own placement-
        accept response returned -- never a fill, price, or status guessed at. This is what closes
        Part 5's entry-correlation gap (see `docs/ORDER_HISTORY_V2_DESIGN.md`): the server has no
        way to reverse [order_tag] back to [idempotency_key] later (it's a one-way hash), so the
        only place that mapping can be recorded is here, at the moment the caller still knows it.

        [role] `"ENTRY"` reuses [idempotency_key] as the eventual `lots.id` once a fill confirms
        it -- stable and unique per placement, no separate id-generation scheme needed. `status`
        is written as `"submitted"` -- a locally-known fact ("we asked Upstox to place this"), not
        a broker-confirmed status; the first real WS-push-triggered re-fetch overwrites it with
        broker truth via [record_order_snapshot]'s own upsert-by-`broker_order_id`.

        [target_price]/[stoploss_price]/[trailing_gap] (§6.3 Part B2): the bracket this entry
        placement itself intends, carried on this same correlation row purely so it survives to
        fill time -- `_record_order_history_from_push` (app.main) reads them back off this row
        when the fill confirms and hands them to [apply_fill_to_ledger], which arms the bracket via
        [arm_lot_bracket] in the same pass that creates the `Lot`. All three `None` (every
        pre-existing caller, and any `"EXIT"`/`"MANUAL"` placement) means no bracket to carry."""
        return self._ledger_store.upsert_order(
            id=str(uuid4()),
            broker_order_id=broker_order_id,
            exchange_order_id=None,
            idempotency_key=idempotency_key,
            order_tag=order_tag,
            instrument_key=instrument_key,
            trading_symbol=None,
            transaction_type=transaction_type,
            product=product,
            order_type=order_type,
            requested_quantity=requested_quantity,
            requested_price=requested_price,
            trigger_price=trigger_price,
            status="submitted",
            status_message=None,
            average_price=None,
            filled_quantity=0,
            lot_id=None,
            rule_id=None,
            role=role,
            placed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            last_broker_update_at=None,
            raw_broker_payload_json=None,
            target_price=target_price,
            stoploss_price=stoploss_price,
            trailing_gap=trailing_gap,
        )

    def record_order_snapshot(
        self,
        broker_order: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        order_tag: Optional[str] = None,
        lot_id: Optional[str] = None,
        rule_id: Optional[str] = None,
        role: Optional[str] = None,
    ) -> dict[str, Any]:
        """Upserts one `order_history` row for [broker_order], keyed by its own `order_id`.
        Called for *every* reconciled order sighting -- placement-accept, open, rejected,
        cancelled, complete -- so `order_history` genuinely records every order ever placed, not
        just the ones that filled."""
        broker_order_id = broker_order.get("order_id")
        if not broker_order_id:
            raise ValueError("broker_order missing order_id -- nothing to key the row on")

        return self._ledger_store.upsert_order(
            id=str(uuid4()),
            broker_order_id=str(broker_order_id),
            exchange_order_id=broker_order.get("exchange_order_id"),
            idempotency_key=idempotency_key,
            order_tag=order_tag or broker_order.get("tag"),
            instrument_key=str(broker_order.get("instrument_token") or broker_order.get("instrument_key")),
            trading_symbol=broker_order.get("trading_symbol"),
            transaction_type=str(broker_order.get("transaction_type", "")).upper(),
            product=str(broker_order.get("product", "I")),
            order_type=str(broker_order.get("order_type", "MARKET")),
            requested_quantity=int(broker_order.get("quantity") or 0),
            requested_price=_number_or_none(broker_order.get("price")),
            trigger_price=_number_or_none(broker_order.get("trigger_price")),
            status=str(broker_order.get("status", "unknown")),
            status_message=broker_order.get("status_message"),
            average_price=_number_or_none(broker_order.get("average_price")),
            filled_quantity=int(broker_order.get("filled_quantity") or 0),
            lot_id=lot_id,
            rule_id=rule_id,
            role=role,
            placed_at=broker_order.get("order_timestamp"),
            last_broker_update_at=broker_order.get("exchange_timestamp") or broker_order.get("order_timestamp"),
            raw_broker_payload_json=_to_json(broker_order),
        )

    def apply_fill_to_ledger(
        self,
        broker_order: dict[str, Any],
        *,
        lot_id: Optional[str],
        role: str,
        entry_transaction_type: Optional[str] = None,
        target_price: Optional[float] = None,
        stoploss_price: Optional[float] = None,
        trailing_gap: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Only meaningful for a `status == "complete"` broker order with a resolved [role]
        (`"ENTRY"`/`"EXIT"` -- `"MANUAL"`/`None` never mutates `lots`, per Part 5's "record every
        order, but only real lot-workflow fills mutate positions" design). Returns the mutated
        lot row, or `None` if there was nothing to do.

        [target_price]/[stoploss_price]/[trailing_gap] (§6.3 Part B2, `ENTRY` only): the intended
        bracket, forwarded straight to [_apply_entry_fill] -- see that method's own doc comment for
        when it actually arms anything."""
        if role not in ("ENTRY", "EXIT"):
            return None

        average_price = _number_or_none(broker_order.get("average_price"))
        filled_quantity = int(broker_order.get("filled_quantity") or 0)
        if average_price is None or filled_quantity <= 0:
            return None

        instrument_key = str(broker_order.get("instrument_token") or broker_order.get("instrument_key"))
        transaction_type = str(broker_order.get("transaction_type", "")).upper()

        if role == "ENTRY":
            return self._apply_entry_fill(
                lot_id=lot_id,
                instrument_key=instrument_key,
                transaction_type=transaction_type,
                average_price=average_price,
                filled_quantity=filled_quantity,
                target_price=target_price,
                stoploss_price=stoploss_price,
                trailing_gap=trailing_gap,
            )

        return self._apply_exit_fill(
            lot_id=lot_id,
            average_price=average_price,
            filled_quantity=filled_quantity,
            entry_transaction_type=entry_transaction_type,
        )

    def _apply_entry_fill(
        self,
        *,
        lot_id: Optional[str],
        instrument_key: str,
        transaction_type: str,
        average_price: float,
        filled_quantity: int,
        target_price: Optional[float] = None,
        stoploss_price: Optional[float] = None,
        trailing_gap: Optional[float] = None,
    ) -> dict[str, Any]:
        existing = self._ledger_store.get_lot(lot_id) if lot_id else None
        if existing is None:
            new_lot_id = lot_id or str(uuid4())
            lot = self._ledger_store.upsert_lot(
                lot_id=new_lot_id,
                instrument_key=instrument_key,
                transaction_type=transaction_type,
                entry_price=average_price,
                entry_quantity=filled_quantity,
                remaining_quantity=filled_quantity,
                realized_pnl=0.0,
                state="OPEN",
            )
            # §6.3 Part B2: arm the bracket in this same pass, exactly once, right where the lot
            # itself is born -- same "arm in the same write" posture LotRepository.createLot
            # already follows client-side. Deliberately not on the re-averaging branch below (a
            # second entry order building on an already-open lot never re-arms) -- an already-armed
            # lot's bracket is managed from here on by the trigger evaluator/manual modify paths,
            # not by a later entry fill.
            return arm_lot_bracket(
                self._ledger_store, lot,
                target_price=target_price, stoploss_price=stoploss_price, trailing_gap=trailing_gap,
            )

        # A second entry order building the same still-open lot -- quantity-weighted re-average,
        # never a flat overwrite of entry_price.
        prior_price = existing.get("entry_price") or 0.0
        prior_quantity = existing.get("entry_quantity") or 0
        new_entry_quantity = prior_quantity + filled_quantity
        new_entry_price = (
            (prior_price * prior_quantity) + (average_price * filled_quantity)
        ) / new_entry_quantity if new_entry_quantity else average_price
        new_remaining = (existing.get("remaining_quantity") or 0) + filled_quantity

        return self._ledger_store.upsert_lot(
            lot_id=existing["id"],
            instrument_key=existing["instrument_key"],
            transaction_type=existing["transaction_type"],
            entry_price=new_entry_price,
            entry_quantity=new_entry_quantity,
            remaining_quantity=new_remaining,
            realized_pnl=existing.get("realized_pnl") or 0.0,
            state=existing.get("state") or "OPEN",
            target_price=existing.get("target_price"),
            stoploss_price=existing.get("stoploss_price"),
            trailing_gap=existing.get("trailing_gap"),
            target_rule_id=existing.get("target_rule_id"),
            stoploss_rule_id=existing.get("stoploss_rule_id"),
            product=existing.get("product") or "I",
        )

    def _apply_exit_fill(
        self,
        *,
        lot_id: Optional[str],
        average_price: float,
        filled_quantity: int,
        entry_transaction_type: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if not lot_id:
            return None
        lot = self._ledger_store.get_lot(lot_id)
        if lot is None or lot.get("entry_price") is None:
            return None

        pnl_delta = compute_realized_pnl(
            entry_price=lot["entry_price"],
            avg_exit_price=average_price,
            filled_quantity=filled_quantity,
            transaction_type=entry_transaction_type or lot.get("transaction_type", "BUY"),
        )
        new_remaining = max((lot.get("remaining_quantity") or 0) - filled_quantity, 0)
        new_state = "CLOSED" if new_remaining == 0 else (lot.get("state") or "OPEN")

        return self._ledger_store.upsert_lot(
            lot_id=lot["id"],
            instrument_key=lot["instrument_key"],
            transaction_type=lot["transaction_type"],
            entry_price=lot["entry_price"],
            entry_quantity=lot["entry_quantity"],
            remaining_quantity=new_remaining,
            realized_pnl=(lot.get("realized_pnl") or 0.0) + pnl_delta,
            state=new_state,
            target_price=lot.get("target_price"),
            stoploss_price=lot.get("stoploss_price"),
            trailing_gap=lot.get("trailing_gap"),
            target_rule_id=lot.get("target_rule_id"),
            stoploss_rule_id=lot.get("stoploss_rule_id"),
            product=lot.get("product") or "I",
        )


def _number_or_none(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)

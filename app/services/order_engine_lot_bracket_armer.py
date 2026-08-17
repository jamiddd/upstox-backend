from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

from app.services.order_engine_ledger_store import OrderEngineLedgerStore

"""§6.3 Part B2's server-side bracket armer -- the direct port of the Android client's
`LotBracketRuleBuilder`, applied the moment the server itself creates a fresh `Lot` from a
confirmed entry fill (`OrderHistoryRecorder._apply_entry_fill`), rather than waiting for the client
to mirror its own locally-armed rules over `PUT /order-engine/ledger/trigger-rules`. This is the
piece that actually closes the audit's original finding: a disconnected/backgrounded/killed phone
no longer means an unprotected position, because the server arms the bracket itself, in the same
fill-handling pass that creates the lot -- no client round trip required.

Direction logic mirrors `LotBracketRuleBuilder.build`/`buildRule` exactly: a long lot (`BUY` entry)
exits `SELL`, target fires `ABOVE`, stop-loss fires `BELOW`; a short lot (`SELL` entry) is the
mirror image. Both legs, when both prices are given, are linked as OCO siblings via
`sibling_rule_id` -- a one-sided bracket (only one of target/stoploss price given) has nothing to
cancel when it fires, same as the Android side.
"""


def arm_lot_bracket(
    ledger_store: OrderEngineLedgerStore,
    lot: dict[str, Any],
    *,
    target_price: Optional[float],
    stoploss_price: Optional[float],
    trailing_gap: Optional[float],
) -> dict[str, Any]:
    """Arms [lot] with real `trigger_rules` rows for whichever of [target_price]/[stoploss_price]
    is given, then updates [lot] itself to carry the resulting rule ids/bracket prices. Returns the
    updated lot row unchanged (a no-op, but still returns the current row) if neither price is
    given -- same "nothing to build" short-circuit `LotBracketRuleBuilder.build` uses. Callers
    should only invoke this once per lot, at creation -- arming an already-armed lot a second time
    would create a duplicate, disconnected pair of trigger_rules with no lot-side pointer to the
    old ones; that guard is the caller's responsibility (`_apply_entry_fill` only calls this on the
    branch that just created a brand-new lot), same division of responsibility
    `LotRepository.createLot`/`LotBracketArmer` split on the Android side.
    """
    if target_price is None and stoploss_price is None:
        return lot

    # Unlike the Android client's TriggerRule, the server's own rule row carries no
    # action/payload -- _fire_rule (order_engine_trigger_evaluator.py) derives the exit
    # transaction_type straight from the lot's own transaction_type at fire time, so there's no
    # exit-side field to compute or store here.
    is_long = str(lot.get("transaction_type", "")).upper() == "BUY"

    target_rule_id = str(uuid4()) if target_price is not None else None
    stoploss_rule_id = str(uuid4()) if stoploss_price is not None else None

    if target_rule_id is not None:
        ledger_store.upsert_trigger_rule(
            rule_id=target_rule_id,
            lot_id=lot["id"],
            instrument_key=lot["instrument_key"],
            role="TARGET",
            state="ARMED",
            condition_op="ABOVE" if is_long else "BELOW",
            condition_value=target_price,
            sibling_rule_id=stoploss_rule_id,
        )
    if stoploss_rule_id is not None:
        ledger_store.upsert_trigger_rule(
            rule_id=stoploss_rule_id,
            lot_id=lot["id"],
            instrument_key=lot["instrument_key"],
            role="STOP_LOSS",
            state="ARMED",
            condition_op="BELOW" if is_long else "ABOVE",
            condition_value=stoploss_price,
            sibling_rule_id=target_rule_id,
        )

    return ledger_store.upsert_lot(
        lot_id=lot["id"],
        instrument_key=lot["instrument_key"],
        transaction_type=lot["transaction_type"],
        entry_price=lot["entry_price"],
        entry_quantity=lot["entry_quantity"],
        remaining_quantity=lot["remaining_quantity"],
        realized_pnl=lot.get("realized_pnl") or 0.0,
        state=lot.get("state") or "OPEN",
        target_price=target_price,
        stoploss_price=stoploss_price,
        trailing_gap=trailing_gap,
        target_rule_id=target_rule_id,
        stoploss_rule_id=stoploss_rule_id,
        product=lot.get("product") or "I",
    )

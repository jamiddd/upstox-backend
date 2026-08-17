from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional, Protocol
from zoneinfo import ZoneInfo

from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.core.market_hours import is_market_open
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker
from app.services.order_engine_order_service import OrderEngineOrderService

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_FALLBACK_LOOP_INTERVAL_SECONDS = 5.0

# §6.4 Part B4: "the server engine stamps last_evaluated_at somewhere the client observes ... every
# evaluation cycle" -- this is that stamp. Module-level, process-lifetime state (this backend has
# exactly one evaluator, same posture OrderEngineLotTracker's own _last_ltp cache already uses) --
# stamped at the top of both check_now (tick-driven) and run_fallback_loop's own loop body (its
# 5s-interval backstop), so the timestamp advances as long as *either* path is alive, independent
# of whether any lot happens to be open right now. GET /order-engine/engine-health is what exposes
# this to the client's own EngineHeartbeatMonitor.
_last_evaluated_at: Optional[datetime] = None


def last_evaluated_at() -> Optional[datetime]:
    return _last_evaluated_at


def _stamp_evaluated(now: datetime) -> None:
    global _last_evaluated_at
    _last_evaluated_at = now

"""§6.3/§8's server-side bracket executor (`docs/ORDER_POSITION_OVERHAUL_DESIGN.md`) -- the concrete
answer to the audit finding that a disconnected/backgrounded/killed phone left every open lot's
bracket completely unwatched, since the Android client's own `TriggerEvaluator` was the *only*
thing evaluating triggers at all (`OrderEngineLedgerStore`'s own header comment used to say so
explicitly: "a record, not an evaluator ... the client-side TriggerEvaluator still makes the actual
fire decision"). This module is what makes that no longer true.

Mirrors `order_engine_max_loss_watcher.py`'s own two-layer shape exactly (tick-driven [check_now] +
a fixed-interval [run_fallback_loop] backstop for stretches with no live ticks), and mirrors the
Android client's own `TriggerEvaluator`/`TriggerExecutor`/`TriggerStateMachine` trio's *logic*
(condition matching, `ARMED -> FIRING -> PLACED/FAILED`, OCO sibling-cancel, role priority) even
though this is a different language -- kept in sync by hand if either side's own rules ever change.

**v1 scope, deliberately narrower than the Android client's own evaluator, named here rather than
left silent:**
- `ConditionOp.CROSSES` is not evaluated -- only `ABOVE`/`BELOW` (a rule using `CROSSES` never
  fires from this module; every bracket leg armed via `LotBracketRuleBuilder`, both client- and
  once B2 ships, server-side, only ever uses `ABOVE`/`BELOW`, so this is not yet a live gap, but is
  worth closing before `CROSSES` is used anywhere).
- No trailing-stop ratchet (`TrailingStopCalculator`'s server-side twin) -- a `trailing_gap` on a
  lot is not applied here yet; `stoploss_price` stays fixed at whatever it was armed with.
- Exit orders are always `MARKET`, never the client's own dynamic-spread `LIMIT` shaping
  (`DynamicSpreadExitPricer`) -- same considered "immediate execution over shaped price" tradeoff
  `order_engine_max_loss_watcher.flatten_open_lots` already makes for its own emergency exits,
  applied here too since a fired bracket leg is exactly that kind of urgent exit.
These are acceptable interim gaps, not correctness bugs -- every trade this module protects still
gets a real stop-loss/target fired at the right price crossing; it just doesn't yet cover every
condition shape/pricing refinement the client side does.
"""


class _TokenStoreProtocol(Protocol):
    def has_token(self) -> bool: ...
    def load_access_token(self) -> str: ...


class _NotificationServiceProtocol(Protocol):
    async def record(self, *, category: str, severity: str, title: str, message: str, details: Any = None) -> None: ...


# STOP_LOSS before TARGET before an unlabeled role -- the same "assume stop-loss fires first"
# worst-outcome-first pessimism the Android client's TriggerRule.role sort already applies when two
# rules on the same instrument both match the same tick.
_ROLE_PRIORITY = {"STOP_LOSS": 0, "TARGET": 1}


def _role_sort_key(rule: dict) -> int:
    return _ROLE_PRIORITY.get(rule.get("role"), 2)


def _condition_matches(condition_op: Optional[str], condition_value: Optional[float], ltp: float) -> bool:
    if condition_op == "ABOVE":
        return ltp >= (condition_value or 0.0)
    if condition_op == "BELOW":
        return ltp <= (condition_value or 0.0)
    return False  # CROSSES, or an unrecognized op -- see this module's own v1 scope note.


async def _fire_rule(
    rule: dict,
    ltp: float,
    *,
    access_token: str,
    ledger_store: OrderEngineLedgerStore,
    order_engine_order_service: OrderEngineOrderService,
    notification_service: _NotificationServiceProtocol,
) -> None:
    """Fires one already-matched, already-CAS-won [rule] -- places its exit order and moves it to
    `PLACED`/`FAILED`. The CAS to `FIRING` (this function's own precondition, done by its caller in
    [check_now]) is what makes this safe to call from two racing ticks for the same instrument:
    only the tick that actually won that CAS ever reaches here for this rule."""
    lot = ledger_store.get_lot(rule["lot_id"]) if rule.get("lot_id") else None
    if lot is None:
        logger.warning("trigger rule %s fired with no matching lot %s -- marking FAILED", rule["id"], rule.get("lot_id"))
        ledger_store.cas_update_trigger_rule_state(rule["id"], rule["version"], "FAILED")
        return

    entry_transaction_type = str(lot.get("transaction_type")).upper()
    exit_transaction_type = "SELL" if entry_transaction_type == "BUY" else "BUY"
    remaining_quantity = lot.get("remaining_quantity") or 0

    if remaining_quantity <= 0:
        logger.info("trigger rule %s fired against lot %s with nothing remaining -- marking FAILED", rule["id"], lot["id"])
        ledger_store.cas_update_trigger_rule_state(rule["id"], rule["version"], "FAILED")
        return

    try:
        await order_engine_order_service.place_order(
            access_token,
            idempotency_key=rule["id"],
            instrument_key=rule["instrument_key"],
            transaction_type=exit_transaction_type,
            quantity=int(remaining_quantity),
            product=str(lot.get("product") or "I"),
            order_type="MARKET",
        )
    except Exception:
        # Deliberately broad -- same "this leg's own placement didn't go out" posture
        # order_engine_max_loss_watcher.flatten_open_lots already uses for its own exits. Marked
        # FAILED rather than left FIRING forever; a future reconciliation pass (mirroring the
        # Android client's own TriggerReconciler, not yet ported) is what would retry/escalate a
        # genuinely ambiguous failure -- see this module's own v1 scope note.
        logger.warning("trigger rule %s exit placement failed", rule["id"], exc_info=True)
        ledger_store.cas_update_trigger_rule_state(rule["id"], rule["version"], "FAILED")
        await notification_service.record(
            category="risk",
            severity="critical",
            title="Server-side bracket leg failed to place",
            message=(
                f"Trigger rule {rule['id']} ({rule.get('role')}) matched at LTP {ltp:.2f} for lot "
                f"{lot['id']} but its exit order failed to place -- this lot may now be unprotected."
            ),
        )
        return

    placed = ledger_store.cas_update_trigger_rule_state(rule["id"], rule["version"], "PLACED")
    if placed is None:
        # Shouldn't happen -- this rule's version was exclusively ours from the FIRING CAS onward,
        # nothing else transitions a FIRING rule. Logged, not raised: the broker order already
        # went out for real, so there is nothing left to roll back.
        logger.warning("trigger rule %s: order placed but PLACED CAS unexpectedly lost its race", rule["id"])

    ledger_store.record_event(
        lot_id=lot["id"], rule_id=rule["id"], event_type="SERVER_TRIGGER_FIRED",
        payload={"ltp": ltp, "role": rule.get("role"), "exit_transaction_type": exit_transaction_type},
    )

    sibling_id = rule.get("sibling_rule_id")
    if sibling_id:
        sibling = ledger_store.get_trigger_rule(sibling_id)
        # Only cancel a sibling still ARMED -- it was never placed at the broker (a bracket leg
        # only reaches the broker once it fires), so this is purely a local state transition, no
        # broker cancel call needed, exactly like the Android client's own OCO cancel.
        if sibling is not None and sibling.get("state") == "ARMED":
            ledger_store.cas_update_trigger_rule_state(sibling_id, sibling["version"], "CANCELLED")

    logger.info("trigger rule %s (%s) fired at LTP %.2f for lot %s", rule["id"], rule.get("role"), ltp, lot["id"])


async def check_now(
    instrument_key: str,
    ltp: Optional[float],
    *,
    now: datetime,
    token_store: _TokenStoreProtocol,
    ledger_store: OrderEngineLedgerStore,
    order_engine_order_service: OrderEngineOrderService,
    notification_service: _NotificationServiceProtocol,
) -> None:
    """The actual check -- called from `app.main`'s market-tick handler on every live tick that
    touches an instrument with an open order-engine lot, and by [run_fallback_loop] on a plain
    timer as a backstop for stretches with no ticks. Mirrors
    `order_engine_max_loss_watcher.check_now`'s own shape."""
    _stamp_evaluated(now)  # proves this evaluator was actually invoked, regardless of outcome below
    if ltp is None:
        return
    if not is_market_open(now.astimezone(_IST)):
        return

    armed_rules = ledger_store.get_armed_trigger_rules_for_instrument(instrument_key)
    if not armed_rules:
        return

    access_token: Optional[str] = None
    if token_store.has_token():
        try:
            access_token = token_store.load_access_token()
        except (TokenStoreError, UpstoxAuthRequiredError):
            access_token = None
    if access_token is None:
        return  # Can't place an exit without a token -- same posture check_now's max-loss sibling has.

    for rule in sorted(armed_rules, key=_role_sort_key):
        if not _condition_matches(rule.get("condition_op"), rule.get("condition_value"), ltp):
            continue

        # The real database-level CAS: only the tick that wins this actually fires the rule. A
        # concurrent tick for the same instrument (or the fallback loop's own pass landing at the
        # same moment) that loses this CAS simply moves on -- see cas_update_trigger_rule_state's
        # own doc comment.
        fired = ledger_store.cas_update_trigger_rule_state(rule["id"], rule["version"], "FIRING")
        if fired is None:
            continue

        try:
            await _fire_rule(
                fired, ltp,
                access_token=access_token,
                ledger_store=ledger_store,
                order_engine_order_service=order_engine_order_service,
                notification_service=notification_service,
            )
        except Exception:
            # Deliberately broad -- one rule's own unexpected failure (a bug in this module, not a
            # placement failure -- those are already handled inside _fire_rule) must not stop the
            # rest of this tick's other armed rules from being evaluated.
            logger.exception("trigger rule %s: firing raised unexpectedly", rule["id"])


async def run_fallback_loop(
    *,
    token_store: _TokenStoreProtocol,
    ledger_store: OrderEngineLedgerStore,
    lot_tracker: OrderEngineLotTracker,
    order_engine_order_service: OrderEngineOrderService,
    notification_service: _NotificationServiceProtocol,
) -> None:
    """Background fallback loop -- see [check_now]'s own doc comment for the primary, tick-driven
    reactivity path this backs up. Iterates every instrument with an open lot, using
    [OrderEngineLotTracker.last_ltp] rather than forcing a fresh quote (same "last-known, not
    literally instantaneous" posture that tracker already documents for its own P&L reads) -- an
    instrument with no tick since process start is simply skipped for this pass, same as
    [check_now]'s own `ltp is None` no-op. Mirrors
    `order_engine_max_loss_watcher.run_fallback_loop`'s own shape exactly."""
    while True:
        try:
            # Stamped unconditionally, even with zero open lots -- see this module's own
            # _last_evaluated_at doc comment for why the heartbeat must advance independent of
            # whether check_now below has anything to actually iterate.
            _stamp_evaluated(datetime.now(_IST))
            for instrument_key in ledger_store_instrument_keys(ledger_store):
                await check_now(
                    instrument_key,
                    lot_tracker.last_ltp(instrument_key),
                    now=datetime.now(_IST),
                    token_store=token_store,
                    ledger_store=ledger_store,
                    order_engine_order_service=order_engine_order_service,
                    notification_service=notification_service,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("order-engine trigger evaluator fallback loop pass failed unexpectedly")
        await asyncio.sleep(_FALLBACK_LOOP_INTERVAL_SECONDS)


def ledger_store_instrument_keys(ledger_store: OrderEngineLedgerStore) -> set[str]:
    """Every instrument with at least one currently-open lot -- the fallback loop's own iteration
    set. A tiny wrapper (not `OrderEngineLotTracker.instrument_keys`, which only reports what it
    already tracks) so the fallback loop still covers a lot that's been open since before this
    process started and simply hasn't ticked yet."""
    return {lot["instrument_key"] for lot in ledger_store.get_open_lots()}

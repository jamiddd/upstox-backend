from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.core.market_hours import is_market_open
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker
from app.services.order_engine_order_service import OrderEngineOrderService

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_FALLBACK_LOOP_INTERVAL_SECONDS = 5.0

"""§8.3/§8.4 milestone 6's server-side max-loss enforcer (`docs/ORDER_POSITION_OVERHAUL_DESIGN.md`)
-- the concrete "the server cannot default" mechanism: mirrors the already-existing old-engine
`max_loss_watcher.py`'s two-layer shape (tick-driven `check_now` + a fixed-interval
`run_fallback_loop` backstop for stretches with no live ticks) *exactly*, applied against the new
engine's own `OrderEngineLedgerStore`/`OrderEngineLotTracker` instead of `PositionPnlTracker`, with
a real, server-initiated flatten call -- never dependent on the client's local `MaxLossAggregator`/
`FlattenHandler` at all.

The breach formula itself is the same literal §7.10 trailing-stop-on-capital rule the client's own
`MaxLossCalculator` already implements (`if currentEquity > openingBalance: breach when
currentEquity <= peakEquity - X else breach when currentEquity <= openingBalance - X`) --
duplicated here deliberately (not imported, this is a different language) rather than left
undefined; keep the two in sync by hand if the formula itself ever changes.

**Honest scope note, v1** -- two named limitations, not hidden:
1. `_current_equity`'s unrealized component uses `OrderEngineLotTracker.total_live_pnl()`, which
   falls back to zero live P&L for any open lot whose instrument hasn't ticked yet (see that
   method's own doc comment). That is *not* the worst-case-first pessimism §8.3's own principle
   calls for -- a genuinely pessimistic version would need to assume every un-ticked open lot at
   its worst plausible price (a forced re-quote, or a conservative bound), not a flat zero. This is
   a real, named gap, not something already solved by this milestone.
2. [flatten_open_lots] has no per-lot `product`/`order_type` recorded in the ledger to place an
   exactly-matching exit with (the ledger schema doesn't carry those fields yet) -- it always
   places a plain `MARKET` exit at product `"I"` (intraday), regardless of what the lot's own
   entry/bracket was actually configured with. Broadening the ledger schema to carry that through
   is a follow-up, not this milestone's.
"""


class _LedgerMaxLossSettingsProtocol(Protocol):
    def get_max_loss_epoch(self) -> Any: ...
    def upsert_max_loss_epoch(self, **kwargs: Any) -> Any: ...
    def get_all_lots(self) -> list[dict[str, Any]]: ...
    def get_open_lots(self) -> list[dict[str, Any]]: ...


class _TokenStoreProtocol(Protocol):
    def has_token(self) -> bool: ...
    def load_access_token(self) -> str: ...


class _NotificationServiceProtocol(Protocol):
    async def record(self, *, category: str, severity: str, title: str, message: str, details: Any = None) -> None: ...


def _current_equity(ledger_store: OrderEngineLedgerStore, lot_tracker: OrderEngineLotTracker, opening_balance: float) -> float:
    """`currentEquity = epoch.openingBalance + realizedPnl(all lots) + unrealizedPnl(open lots)`,
    the literal §7.10 formula (`PnLCalculator.snapshot`'s own equivalent), applied against the new
    engine's own ledger instead of the client's local Room DB."""
    total_realized = sum(_number(lot.get("realized_pnl")) for lot in ledger_store.get_all_lots())
    total_unrealized = lot_tracker.total_live_pnl()
    return opening_balance + total_realized + total_unrealized


def _effective_threshold(threshold_x: float, threshold_mode: str, reference_point: float) -> float:
    """§7.10's 2026-08-12 amendment: `threshold_mode` is `"ABSOLUTE"` (X is a fixed currency
    amount, unchanged from before this amendment) or `"PERCENTAGE"` (X is a percentage of
    [reference_point] -- `peakEquity` in the profit branch, `openingBalance` in the flat-floor
    branch -- computed fresh on every check, never cached, so a percentage-mode cushion tracks
    whatever the reference point currently is, including after an auto-re-arm shrinks it)."""
    if threshold_mode == "PERCENTAGE":
        return (threshold_x / 100.0) * reference_point
    return threshold_x


def _is_breached(current_equity: float, opening_balance: float, peak_equity: float, threshold_x: float, threshold_mode: str = "ABSOLUTE") -> bool:
    if current_equity > opening_balance:
        return current_equity <= peak_equity - _effective_threshold(threshold_x, threshold_mode, peak_equity)
    return current_equity <= opening_balance - _effective_threshold(threshold_x, threshold_mode, opening_balance)


_IN_FLIGHT_EXIT_STATES = {"EVALUATING", "FIRING", "PLACED"}


def _lot_has_exit_in_flight(ledger_store: OrderEngineLedgerStore, lot: dict) -> bool:
    """True if either of [lot]'s own bracket legs (`target_rule_id`/`stoploss_rule_id`) is
    currently `EVALUATING`/`FIRING`/`PLACED` -- i.e. the trigger engine has *already* fired (or is
    in the process of firing) a real exit order for this lot, resting or in flight at the broker
    right now. Guards a genuine race this watcher would otherwise cause: a lot's own stop-loss
    firing (a resting `LIMIT`/`MARKET` order already `PLACED`) at the same moment a portfolio-wide
    max-loss breach is also detected -- without this check, [flatten_open_lots] would place a
    *second*, independent exit order for the same quantity under a different idempotency key,
    risking both filling and over-exiting into a short/long the user never intended. `ARMED`
    (not yet fired) and terminal states (`CANCELLED`/`FAILED`/`EXPIRED`) are not in-flight -- a
    lot whose bracket hasn't fired yet, or whose attempt already failed/was cancelled, still needs
    the flatten to protect it."""
    for rule_id in (lot.get("target_rule_id"), lot.get("stoploss_rule_id")):
        if not rule_id:
            continue
        rule = ledger_store.get_trigger_rule(rule_id)
        if rule is not None and rule.get("state") in _IN_FLIGHT_EXIT_STATES:
            return True
    return False


async def flatten_open_lots(
    access_token: str,
    ledger_store: OrderEngineLedgerStore,
    order_engine_order_service: OrderEngineOrderService,
) -> list[str]:
    """Places a fresh-idempotency-key `MARKET` exit for every open lot's `remaining_quantity`,
    same "flatten only, never blocks new entries" posture §7.10 specifies -- **except**:
    - a lot that already has an exit in flight via its own armed bracket (see
      [_lot_has_exit_in_flight]'s own doc comment), which is skipped rather than double-exited;
    - the *quantity itself* is capped at [OrderEngineOrderService.resolve_closeable_quantity]'s
      broker-truth re-confirmation right before placing, never the ledger's own `remaining_quantity`
      blindly -- the same "held quantity, not a stale local number" discipline
      `guard_against_unintended_short` uses for the manual entry screen, applied here as this
      race's second line of defense (the in-flight check is the first; this catches the case
      where the ledger's sync lag itself is what let the race through -- see that check's own
      "residual risk" doc comment). A lot with nothing genuinely closeable at the broker (already
      closed by something else, or the ledger was simply wrong) is skipped entirely, never sent as
      a zero/negative-quantity order.

    One lot's placement failure doesn't abort the rest -- §8.3's "the server cannot default"
    applies here too: a partial flatten is still far better than none. Returns the lot ids that
    were actually placed (never raises); a skipped lot (in-flight exit, or nothing closeable) is
    not included, but is also not a failure."""
    placed_lot_ids: list[str] = []
    for lot in ledger_store.get_open_lots():
        remaining_quantity = lot.get("remaining_quantity") or 0
        if remaining_quantity <= 0:
            continue
        if _lot_has_exit_in_flight(ledger_store, lot):
            logger.info(
                "order-engine max-loss flatten skipping lot %s -- its own bracket leg already "
                "has an exit in flight, avoiding a duplicate/over-exit order",
                lot["id"],
            )
            continue

        entry_transaction_type = str(lot.get("transaction_type")).upper()
        closeable_quantity = await order_engine_order_service.resolve_closeable_quantity(
            access_token, lot["instrument_key"], entry_transaction_type,
        )
        exit_quantity = min(remaining_quantity, closeable_quantity)
        if exit_quantity <= 0:
            logger.info(
                "order-engine max-loss flatten skipping lot %s -- broker reports nothing "
                "closeable for %s (ledger said %s), avoiding placing an exit against a position "
                "that isn't actually there",
                lot["id"], lot["instrument_key"], remaining_quantity,
            )
            continue
        if exit_quantity < remaining_quantity:
            logger.warning(
                "order-engine max-loss flatten capping lot %s's exit to %s (ledger said %s) -- "
                "broker-reported closeable quantity is smaller, ledger was stale",
                lot["id"], exit_quantity, remaining_quantity,
            )

        exit_transaction_type = "SELL" if entry_transaction_type == "BUY" else "BUY"
        idempotency_key = f"maxloss-{lot['id']}-{uuid.uuid4().hex[:8]}"
        try:
            await order_engine_order_service.place_order(
                access_token,
                idempotency_key=idempotency_key,
                instrument_key=lot["instrument_key"],
                transaction_type=exit_transaction_type,
                quantity=int(exit_quantity),
                product="I",
                order_type="MARKET",
            )
            placed_lot_ids.append(lot["id"])
        except Exception:
            # Deliberately broad -- UpstoxApiError, a transport failure, or anything else are all
            # the same "this lot's own exit didn't go out" outcome; none of them should abort the
            # rest of the flatten, per this function's own "partial flatten beats none" doc
            # comment.
            logger.warning("order-engine max-loss flatten failed for lot %s", lot["id"], exc_info=True)
    return placed_lot_ids


async def check_now(
    *,
    now: datetime,
    token_store: _TokenStoreProtocol,
    ledger_store: OrderEngineLedgerStore,
    lot_tracker: OrderEngineLotTracker,
    order_engine_order_service: OrderEngineOrderService,
    notification_service: _NotificationServiceProtocol,
    exit_all_lock: asyncio.Lock,
) -> None:
    """The actual check -- called from `app.main`'s market-tick handler on every live tick (once
    wired; see the module's own Handoff entry for "not yet wired to a live caller"), and by
    [run_fallback_loop] on a plain timer as a backstop for stretches with no ticks. Mirrors
    `max_loss_watcher.check_now`'s own shape exactly."""
    if not is_market_open(now.astimezone(_IST)):
        return

    epoch = ledger_store.get_max_loss_epoch()
    if epoch is None:
        return

    current_equity = _current_equity(ledger_store, lot_tracker, epoch["opening_balance"])
    ratcheted_peak = max(current_equity, epoch["peak_equity"])
    if ratcheted_peak != epoch["peak_equity"]:
        epoch = ledger_store.upsert_max_loss_epoch(
            opening_balance=epoch["opening_balance"],
            peak_equity=ratcheted_peak,
            threshold_x=epoch["threshold_x"],
            threshold_mode=epoch["threshold_mode"],
            epoch_started_at=epoch["epoch_started_at"],
        )

    if not _is_breached(
        current_equity, epoch["opening_balance"], epoch["peak_equity"], epoch["threshold_x"], epoch["threshold_mode"],
    ):
        return

    if not token_store.has_token():
        return
    try:
        access_token = token_store.load_access_token()
    except (TokenStoreError, UpstoxAuthRequiredError):
        return

    async with exit_all_lock:
        # Re-read: another call (a concurrent tick, or the fallback loop) may have already
        # flattened and re-armed a fresh epoch in the moment between the check above and actually
        # acquiring the lock.
        current_epoch = ledger_store.get_max_loss_epoch()
        if current_epoch is None:
            return
        current_equity = _current_equity(ledger_store, lot_tracker, current_epoch["opening_balance"])
        if not _is_breached(
            current_equity, current_epoch["opening_balance"], current_epoch["peak_equity"],
            current_epoch["threshold_x"], current_epoch["threshold_mode"],
        ):
            return

        placed_lot_ids = await flatten_open_lots(access_token, ledger_store, order_engine_order_service)

        threshold_x = current_epoch["threshold_x"]
        threshold_mode = current_epoch["threshold_mode"]
        # Auto re-arm carries the same threshold_x/threshold_mode pair forward unchanged -- a
        # PERCENTAGE-mode epoch stays a percentage of whatever the new reference point turns out
        # to be (the cushion shrinks in step with capital, by design, per §7.10's own amendment),
        # an ABSOLUTE-mode epoch keeps the exact same rupee cushion, exactly as before this
        # amendment.
        ledger_store.upsert_max_loss_epoch(
            opening_balance=current_equity,
            peak_equity=current_equity,
            threshold_x=threshold_x,
            threshold_mode=threshold_mode,
            epoch_started_at=now.isoformat(),
        )
        logger.warning(
            "order-engine max-loss watcher flattened %d open lot(s): equity=%.2f threshold=%.2f",
            len(placed_lot_ids), current_equity, threshold_x,
        )
        await notification_service.record(
            category="risk",
            severity="critical",
            title="Order-engine max-loss auto square-off triggered",
            message=(
                f"Backend detected a max-loss breach (equity {current_equity:.2f} against a "
                f"-{threshold_x:.2f} threshold) and flattened {len(placed_lot_ids)} open lot(s). "
                "A fresh epoch has been auto-armed at the post-flatten equity."
            ),
        )


async def run_fallback_loop(
    *,
    token_store: _TokenStoreProtocol,
    ledger_store: OrderEngineLedgerStore,
    lot_tracker: OrderEngineLotTracker,
    order_engine_order_service: OrderEngineOrderService,
    notification_service: _NotificationServiceProtocol,
    exit_all_lock: asyncio.Lock,
) -> None:
    """Background fallback loop -- see [check_now]'s own doc comment for the primary, tick-driven
    reactivity path this backs up. Only matters when live ticks stop arriving for a stretch;
    otherwise `check_now` fires on every tick well before this loop's own next iteration would.
    Mirrors `max_loss_watcher.run_max_loss_watcher_fallback`'s own shape exactly."""
    while True:
        try:
            await check_now(
                now=datetime.now(_IST),
                token_store=token_store,
                ledger_store=ledger_store,
                lot_tracker=lot_tracker,
                order_engine_order_service=order_engine_order_service,
                notification_service=notification_service,
                exit_all_lock=exit_all_lock,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("order-engine max-loss fallback loop tick failed unexpectedly")
        await asyncio.sleep(_FALLBACK_LOOP_INTERVAL_SECONDS)


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

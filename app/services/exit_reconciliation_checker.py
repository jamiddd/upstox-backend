from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.services.broker_order_lookup import find_order_by_id
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.realized_pnl import compute_realized_pnl
from app.services.upstox_service import UpstoxService

"""§8.2/§8.4 milestone 5's exit reconciliation (`docs/ORDER_POSITION_OVERHAUL_DESIGN.md`) -- the
concrete mechanism behind §8's "collapse a fuzzy three-way reconciliation into two distinct,
diagnosable failure classes" decision.

Because fill confirmation is always broker -> server -> client (the portfolio feed is
server-side, §8.2), server and client both end up computing realized P&L off the *same* eventual
fill data -- so once this service independently re-derives the number straight from broker ground
truth (a fresh order-book re-fetch, never a cached/pushed payload), it *is* "the server's own
number," and the only comparison left with a single independent ground-truth source is:

  - `server_vs_client` mismatch: the ledger's own `realized_pnl` column (written by the client's
    best-effort `upsertLot` mirror, §8.4 milestone 3) disagrees with what this service just
    recomputed straight from broker truth -- a computation bug on the client side (formula drift,
    rounding, a stale local entry price), since both sides are supposed to be running the
    identical formula off the identical inputs.

§8's own design talks about a second failure class, `broker_vs_server`, for when the server's
*own* number (as opposed to a fresh re-derivation) came from something less direct, like a cached
webhook payload. This service always re-derives directly from broker truth rather than trusting
any cached/forwarded payload, so that class of mismatch has no distinct source to disagree with
here yet -- it becomes meaningful once a server-side path exists that computes realized P&L from
the *pushed* webhook payload alone (a future, cheaper "does not re-fetch broker truth every time" optimization),
at which point this checker's ground-truth re-fetch becomes the third leg to compare that shortcut
against too.

**Scope note, v1**: [check_lot] recomputes realized P&L for the *single, fully-closing* exit case
-- `expected_realized_pnl = (broker_avg_price - entry_price) * broker_filled_quantity * sign`,
matching the Android client's own `LotExitApplier` sign convention exactly (`sign = -1` for a
`SELL`-entry/short lot, `+1` for a `BUY`-entry/long lot). It does not yet replicate
`LotExitApplier`'s weighted-average-across-multiple-partial-exit-fills logic -- a lot that closed
across several separate exit fills will not reconcile precisely against this v1's single-fill
recomputation. Extending this to genuinely partial/multi-fill exits is a named follow-up, same
"mechanism proven for the common case first" staging §7.4 itself went through.
"""

_DEFAULT_TOLERANCE = 0.01


@dataclass
class ReconciliationResult:
    lot_id: str
    outcome: str  # "matched" | "server_vs_client_mismatch" | "lot_not_found" | "broker_order_not_found"
    broker_realized_pnl: Optional[float] = None
    ledger_realized_pnl: Optional[float] = None
    detail: Optional[str] = None

    @property
    def is_mismatch(self) -> bool:
        return self.outcome == "server_vs_client_mismatch"


class ExitReconciliationChecker:
    def __init__(self, ledger_store: OrderEngineLedgerStore, upstox: UpstoxService) -> None:
        self._ledger_store = ledger_store
        self._upstox = upstox

    async def check_lot(
        self, access_token: str, lot_id: str, exit_order_id: str,
    ) -> ReconciliationResult:
        """[exit_order_id] is the broker order id for the exit fill being reconciled -- the same
        id `ExitFillListener` (client) already resolved via its own order-history re-fetch. This
        re-fetches independently server-side rather than trusting any client-supplied fill price,
        per §8's "broker ground truth, always re-confirmed, never taken on faith" discipline."""
        lot = self._ledger_store.get_lot(lot_id)
        if lot is None:
            return ReconciliationResult(lot_id=lot_id, outcome="lot_not_found")

        entry_price = lot.get("entry_price")
        if entry_price is None:
            return ReconciliationResult(
                lot_id=lot_id, outcome="lot_not_found",
                detail="lot has no recorded entry_price yet -- nothing to reconcile against",
            )

        broker_order = await self._find_order(access_token, exit_order_id)
        if broker_order is None:
            return ReconciliationResult(lot_id=lot_id, outcome="broker_order_not_found")

        broker_avg_price = _number(broker_order.get("average_price"))
        broker_filled_quantity = _number(broker_order.get("filled_quantity"))
        broker_realized_pnl = compute_realized_pnl(
            entry_price=entry_price,
            avg_exit_price=broker_avg_price,
            filled_quantity=broker_filled_quantity,
            transaction_type=str(lot.get("transaction_type")),
        )

        ledger_realized_pnl = _number(lot.get("realized_pnl"))

        if not _close(broker_realized_pnl, ledger_realized_pnl):
            # Both "server recomputed from broker truth" and "what the ledger row currently holds"
            # disagree -- the ledger row is whatever the client last mirrored (§8.4 milestone 3),
            # so this is specifically a server/client (computation) mismatch, not a payload bug on
            # this service's own recomputation (which used broker ground truth directly).
            self._ledger_store.record_event(
                event_type="RECONCILIATION_MISMATCH",
                lot_id=lot_id,
                payload={
                    "kind": "server_vs_client_mismatch",
                    "broker_realized_pnl": broker_realized_pnl,
                    "ledger_realized_pnl": ledger_realized_pnl,
                    "exit_order_id": exit_order_id,
                },
            )
            return ReconciliationResult(
                lot_id=lot_id,
                outcome="server_vs_client_mismatch",
                broker_realized_pnl=broker_realized_pnl,
                ledger_realized_pnl=ledger_realized_pnl,
                detail=(
                    f"broker-derived realized_pnl {broker_realized_pnl} != ledger's own "
                    f"realized_pnl {ledger_realized_pnl}"
                ),
            )

        self._ledger_store.record_event(
            event_type="RECONCILIATION_MATCHED",
            lot_id=lot_id,
            payload={"broker_realized_pnl": broker_realized_pnl, "exit_order_id": exit_order_id},
        )
        return ReconciliationResult(
            lot_id=lot_id,
            outcome="matched",
            broker_realized_pnl=broker_realized_pnl,
            ledger_realized_pnl=ledger_realized_pnl,
        )

    async def _find_order(self, access_token: str, order_id: str) -> Optional[dict[str, Any]]:
        return await find_order_by_id(self._upstox, access_token, order_id)


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _close(a: float, b: float, tolerance: float = _DEFAULT_TOLERANCE) -> bool:
    return abs(a - b) <= tolerance

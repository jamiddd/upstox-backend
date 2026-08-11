from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.services.order_engine_ledger_store import OrderEngineLedgerStore

"""§8's server-side live-PnL tracker (`docs/ORDER_POSITION_OVERHAUL_DESIGN.md` §8, "Also needed,
mechanically, to make 8.2 work") -- mirrors `PositionPnlTracker`'s existing shape/role for the old
engine's positions, but keyed off the new engine's own `OrderEngineLedgerStore` lots instead of
Upstox's positions endpoint, and using the *identical* formula the client's own `PnLCalculator`
already uses so the two sides can only ever disagree on which LTP each observed, never on the
arithmetic (§8's own framing).

Unlike `PositionPnlTracker` (which caches a REST-fetched pnl/last_price snapshot and only cheaply
adjusts it from there), this queries `OrderEngineLedgerStore` directly on every `apply_tick` call,
scoped to just that instrument's open lots (`get_open_lots_for_instrument`) -- the ledger store is
a local SQLite file, not a network call, so there's no REST-snapshot/refresh split to manage; the
ledger row itself (kept current by `LotRepository`'s own best-effort mirroring, §8.4 milestone 3)
is always the freshest "entry price / remaining quantity" state there is.

`_last_ltp` is a small in-memory cache of the most recent tick seen per instrument, purely so
`total_live_pnl()` can report a whole-portfolio number without needing a fresh tick for every
instrument at the exact moment it's asked -- same "last-known, not literally instantaneous"
posture `PositionPnlTracker._live_ltp` already accepts.
"""


@dataclass
class LotPnlStatus:
    """One open lot's live status -- what `dispatch_order_engine_lot_status` pushes over `/stream`
    and what `order_engine_max_loss_watcher.py` (§8.4 milestone 6) sums for its own breach check."""

    lot_id: str
    instrument_key: str
    entry_price: Optional[float]
    target_price: Optional[float]
    stoploss_price: Optional[float]
    ltp: float
    live_pnl: float
    state: str


def _live_pnl(lot: dict, ltp: float) -> float:
    """Identical formula to the Android client's `PnLCalculator.unrealizedPnl` -- see that class's
    own doc comment. A lot with no recorded `entry_price` yet (fill not confirmed) contributes 0,
    same "not marked-to-market yet, not absent" posture as the client side."""
    entry_price = lot.get("entry_price")
    if entry_price is None:
        return 0.0
    remaining_quantity = lot.get("remaining_quantity") or 0
    sign = -1.0 if str(lot.get("transaction_type")).upper() == "SELL" else 1.0
    return (ltp - entry_price) * remaining_quantity * sign


def _status_for(lot: dict, ltp: float) -> LotPnlStatus:
    return LotPnlStatus(
        lot_id=lot["id"],
        instrument_key=lot["instrument_key"],
        entry_price=lot.get("entry_price"),
        target_price=lot.get("target_price"),
        stoploss_price=lot.get("stoploss_price"),
        ltp=ltp,
        live_pnl=_live_pnl(lot, ltp),
        state=lot["state"],
    )


class OrderEngineLotTracker:
    def __init__(self, ledger_store: OrderEngineLedgerStore) -> None:
        self._ledger_store = ledger_store
        self._last_ltp: dict[str, float] = {}

    def apply_tick(self, instrument_key: str, ltp: Optional[float]) -> list[LotPnlStatus]:
        """Every open lot on [instrument_key], marked to market at [ltp]. Returns an empty list
        (never raises) if there's no open lot on this instrument or [ltp] is `None` -- most ticks
        on the shared market feed belong to instruments with no order-engine lot at all, so this
        is the common, cheap case. Updates [_last_ltp] regardless of whether any lot is currently
        open on this instrument, so a lot armed moments later still has a recent price to start
        from rather than nothing at all."""
        if ltp is None:
            return []
        self._last_ltp[instrument_key] = ltp
        lots = self._ledger_store.get_open_lots_for_instrument(instrument_key)
        return [_status_for(lot, ltp) for lot in lots]

    def total_live_pnl(self) -> float:
        """Sum of every open lot's live P&L, each marked to the most recent tick seen for its own
        instrument (falling back to the lot's own `entry_price`, i.e. zero live P&L, if no tick has
        arrived yet for that instrument at all -- never a stale foreign price). Deliberately
        computed fresh on every call, not maintained as a running total, per §8.3's "the server
        cannot default" principle: a running total risks silently drifting stale if a lot's update
        is ever missed, a fresh sum from the ledger's own current rows cannot.
        """
        total = 0.0
        for lot in self._ledger_store.get_open_lots():
            entry_price = lot.get("entry_price")
            ltp = self._last_ltp.get(lot["instrument_key"], entry_price)
            if ltp is None:
                continue
            total += _live_pnl(lot, ltp)
        return total

    def instrument_keys(self) -> set[str]:
        """Every currently open lot's instrument -- what a subscription manager needs to keep
        subscribed on the shared market feed so [apply_tick] actually gets called for them,
        mirroring `PositionPnlTracker.instrument_keys`'s own role for the old engine."""
        return {lot["instrument_key"] for lot in self._ledger_store.get_open_lots()}

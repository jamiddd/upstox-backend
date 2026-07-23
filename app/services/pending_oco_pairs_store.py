from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.exceptions import PendingOcoPairsStoreError


@dataclass(frozen=True)
class PendingExit:
    """A target+stoploss exit `SmartOrderService.attach_gtt_exits` armed for a position with no
    GTT bracket -- see that method's own doc comment for why only the stoploss is a real live
    order.

    FIX: this used to be a pair of two live order ids (a resting LIMIT target + a resting SL-M
    stoploss). Upstox rejected the *second* of those two live sell orders outright: placing a
    LIMIT sell for the full held quantity reserves all of it against that order, so a second SELL
    order for the same quantity has nothing left to "cover" it and gets margin-checked as a brand
    new naked short (the "You need to add Rs. X" rejection) -- there is no way to have two live
    sell orders each covering the full position at once. Only [stoploss_order_id] is ever a real
    Upstox order now; [target_trigger_price] is a price level `oco_watcher` itself watches
    (against live quotes, not a broker-side conditional), and only becomes a real MARKET sell
    order once price actually crosses it -- see that module's own doc comment.
    """

    stoploss_order_id: str
    instrument_key: str
    exit_transaction_type: str  # "BUY" or "SELL" -- the exit side
    quantity: int
    product: str
    target_trigger_price: float


class PendingOcoPairsStore:
    """Persists [PendingExit]s awaiting reconciliation by `oco_watcher.run_oco_watcher`, so the
    watch list survives a server/container restart -- same "small flat JSON file, database-free"
    posture as [TrackedInstrumentsStore]. Not sensitive (just order ids/prices), so plain JSON,
    not Fernet-encrypted, same as that store.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.pending_oco_pairs_path)

    def load(self) -> list[PendingExit]:
        """Return the persisted pending exits, or an empty list if nothing's been saved yet (or
        the file is unreadable/corrupt -- degrades to "nothing pending", not a crash, same
        "missing data means omit" posture as the rest of this backend)."""
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw_pairs = payload.get("pairs") if isinstance(payload, dict) else None
        if not isinstance(raw_pairs, list):
            return []
        pending_exits: list[PendingExit] = []
        for raw in raw_pairs:
            if not isinstance(raw, dict):
                continue
            stoploss_order_id = raw.get("stoploss_order_id")
            instrument_key = raw.get("instrument_key")
            exit_transaction_type = raw.get("exit_transaction_type")
            quantity = raw.get("quantity")
            product = raw.get("product")
            target_trigger_price = raw.get("target_trigger_price")
            if (
                isinstance(stoploss_order_id, str)
                and isinstance(instrument_key, str)
                and isinstance(exit_transaction_type, str)
                and isinstance(quantity, int)
                and isinstance(product, str)
                and isinstance(target_trigger_price, (int, float))
            ):
                pending_exits.append(
                    PendingExit(
                        stoploss_order_id=stoploss_order_id,
                        instrument_key=instrument_key,
                        exit_transaction_type=exit_transaction_type,
                        quantity=quantity,
                        product=product,
                        target_trigger_price=float(target_trigger_price),
                    )
                )
        return pending_exits

    def save(self, pending_exits: list[PendingExit]) -> None:
        """Replaces the whole persisted set."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.write_text(
                json.dumps({"pairs": [asdict(pending_exit) for pending_exit in pending_exits]}),
                encoding="utf-8",
            )
        except OSError as exc:
            raise PendingOcoPairsStoreError("Unable to write pending-OCO-pairs store") from exc

    def add(self, pending_exit: PendingExit) -> None:
        """Appends one pending exit to whatever's already persisted."""
        self.save(self.load() + [pending_exit])

    def remove(self, pending_exits_to_remove: list[PendingExit]) -> None:
        """Drops resolved pending exits (stoploss filled/cancelled/rejected, or the target fired)
        from the persisted set. Ignores any entry not actually present -- lets `oco_watcher`
        remove-then-not-worry about a concurrent modification between its own load() and this
        call.
        """
        if not pending_exits_to_remove:
            return
        remaining = [
            pending_exit for pending_exit in self.load() if pending_exit not in pending_exits_to_remove
        ]
        self.save(remaining)


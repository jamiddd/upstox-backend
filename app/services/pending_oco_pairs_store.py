from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.exceptions import PendingOcoPairsStoreError


@dataclass(frozen=True)
class OcoPair:
    """One target+stoploss order pair placed by SmartOrderService.attach_exit_orders for a
    position with no GTT bracket -- see that method's own doc comment for why these are plain
    orders instead of a GTT MULTIPLE order. Watched by `oco_watcher` until one leg fills, at
    which point the other is cancelled and the pair is dropped.
    """

    target_order_id: str
    stoploss_order_id: str
    instrument_key: str


class PendingOcoPairsStore:
    """Persists [OcoPair]s awaiting reconciliation by `oco_watcher.run_oco_watcher`, so the
    watch list survives a server/container restart -- same "small flat JSON file, database-free"
    posture as [TrackedInstrumentsStore]. Not sensitive (just order ids), so plain JSON, not
    Fernet-encrypted, same as that store.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.pending_oco_pairs_path)

    def load(self) -> list[OcoPair]:
        """Return the persisted pairs, or an empty list if nothing's been saved yet (or the file
        is unreadable/corrupt -- degrades to "nothing pending", not a crash, same "missing data
        means omit" posture as the rest of this backend)."""
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw_pairs = payload.get("pairs") if isinstance(payload, dict) else None
        if not isinstance(raw_pairs, list):
            return []
        pairs: list[OcoPair] = []
        for raw in raw_pairs:
            if not isinstance(raw, dict):
                continue
            target_order_id = raw.get("target_order_id")
            stoploss_order_id = raw.get("stoploss_order_id")
            instrument_key = raw.get("instrument_key")
            if (
                isinstance(target_order_id, str)
                and isinstance(stoploss_order_id, str)
                and isinstance(instrument_key, str)
            ):
                pairs.append(
                    OcoPair(
                        target_order_id=target_order_id,
                        stoploss_order_id=stoploss_order_id,
                        instrument_key=instrument_key,
                    )
                )
        return pairs

    def save(self, pairs: list[OcoPair]) -> None:
        """Replaces the whole persisted set."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.write_text(
                json.dumps({"pairs": [asdict(pair) for pair in pairs]}),
                encoding="utf-8",
            )
        except OSError as exc:
            raise PendingOcoPairsStoreError("Unable to write pending-OCO-pairs store") from exc

    def add(self, pair: OcoPair) -> None:
        """Appends one pair to whatever's already persisted."""
        self.save(self.load() + [pair])

    def remove(self, pairs_to_remove: list[OcoPair]) -> None:
        """Drops resolved pairs (either leg filled/cancelled/rejected) from the persisted set.
        Ignores any pair not actually present -- lets `oco_watcher` remove-then-not-worry about
        a concurrent modification between its own load() and this call.
        """
        if not pairs_to_remove:
            return
        remaining = [pair for pair in self.load() if pair not in pairs_to_remove]
        self.save(remaining)
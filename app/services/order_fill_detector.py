from __future__ import annotations

from collections.abc import Collection
from typing import Optional


class OrderFillDetector:
    """Stateful edge detector for today's completed order IDs. Direct port of the Android app's
    own `OrderFillDetector.kt`.

    The first observation is a silent baseline so a backend restart (or the app reconnecting)
    does not replay every fill from earlier in the day. A later observation returns True once if
    it contains any ID absent from all previous observations; multiple new slice IDs in one
    update still produce one event.
    """

    def __init__(self) -> None:
        self._known_completed_ids: Optional[set[str]] = None

    def observe(self, completed_ids: Collection[str]) -> bool:
        current = set(completed_ids)
        known = self._known_completed_ids
        if known is None:
            self._known_completed_ids = current
            return False
        has_new_fill = any(order_id not in known for order_id in current)
        known |= current
        return has_new_fill

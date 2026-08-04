from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from app.core.config import Settings


class GttHistoryStore:
    """Durable local archive of every GTT order status Upstox has ever reported for this account.

    Upstox's own /gtt/orders response is the live source of truth but isn't durable: an order
    ages out of it eventually, and callers of this backend mostly want a filtered ("active only")
    view of it (see SmartOrderService.filter_gtt_orders). This store exists so a caller that wants
    history (include_history=true on GET /orders/gtt) can still see an order Upstox has stopped
    listing, or a bracket that was cancelled out from under a just-flattened position (see
    SmartOrderService._cancel_stray_gtts) -- as long as every call archives the *unfiltered* order
    list (see SmartOrderService.get_all_gtt_orders), not just whatever a given response happened
    to return, each status transition (including into a terminal one) stays observed here.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.gtt_database_path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Local test/dev environments may retain the container default /data path, which
            # isn't writable (or doesn't exist) outside the container -- same fallback posture as
            # this store's callers use tmp_path/an explicit path in tests instead, in practice.
            self.path = Path("/tmp/gtt_history.sqlite3")
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS gtt_history (
                    gtt_order_id TEXT PRIMARY KEY,
                    instrument_token TEXT,
                    status TEXT,
                    payload TEXT NOT NULL,
                    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def archive(self, orders: list[dict[str, Any]]) -> None:
        """Upserts every order by gtt_order_id -- last_seen/status/payload always move forward to
        whatever was just observed, so a status transition (e.g. ACTIVE -> CANCELLED) overwrites
        the stale prior row instead of leaving it behind. Orders without a gtt_order_id are
        skipped; there's nothing stable to key them on.
        """
        with self._connect() as connection:
            for order in orders:
                order_id = order.get("gtt_order_id")
                if not isinstance(order_id, str) or not order_id:
                    continue
                connection.execute(
                    """
                    INSERT INTO gtt_history (gtt_order_id, instrument_token, status, payload)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(gtt_order_id) DO UPDATE SET
                        instrument_token = excluded.instrument_token,
                        status = excluded.status,
                        payload = excluded.payload,
                        last_seen = CURRENT_TIMESTAMP
                    """,
                    (order_id, order.get("instrument_token"), order.get("status"), json.dumps(order)),
                )

    def list(self, instrument_key: Optional[str] = None) -> list[dict[str, Any]]:
        """Every archived order, most-recently-first-seen first, optionally narrowed to one
        instrument. Callers still need to apply their own status filtering (see
        SmartOrderService.filter_gtt_orders) -- this returns the raw archive, terminal statuses
        included.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM gtt_history WHERE ? IS NULL OR instrument_token = ? "
                "ORDER BY first_seen DESC",
                (instrument_key, instrument_key),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

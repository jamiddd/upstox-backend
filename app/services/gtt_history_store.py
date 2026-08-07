from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from app.core.config import Settings


class GttHistoryStore:
    """The authoritative local record of every GTT order this backend has placed, modified, or
    cancelled -- NOT merely a side-archive of Upstox's own list responses.

    Upstox's own GET /order/gtt list endpoint is unreliable in practice: a placed order can
    simply never appear in it. Depending on that list to *discover* that an order exists (the
    old design here) meant a resting order could go completely unrecorded forever if the list
    never happened to include it. So place/modify/cancel now write directly to this store the
    moment Upstox's own place/modify/cancel response confirms them (see record_placed/
    record_modified/record_cancelled) -- no list call is ever in that path. Upstox's list is only
    still consulted afterward, in the background (see gtt_status_poller.py), to learn whether an
    order we already know about has since fired/expired -- via archive(), which only ever upserts
    whatever a given list response *positively* contains; it never removes or marks-terminal a
    row just because that row's id happened to be absent from one call's response.

    archive()/list() remain for the include_history=true view and as a harmless best-effort
    secondary source (e.g. observing a bracket placed from another client) -- but they're no
    longer the only way a row gets written here, which is the actual fix.
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

    def _upsert(
        self, gtt_order_id: str, instrument_token: Optional[str], status: Optional[str], payload: dict[str, Any]
    ) -> None:
        with self._connect() as connection:
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
                (gtt_order_id, instrument_token, status, json.dumps(payload)),
            )

    def archive(self, orders: list[dict[str, Any]]) -> None:
        """Upserts every order by gtt_order_id -- last_seen/status/payload always move forward to
        whatever was just observed, so a status transition (e.g. ACTIVE -> CANCELLED) overwrites
        the stale prior row instead of leaving it behind. Orders without a gtt_order_id are
        skipped; there's nothing stable to key them on. Secondary/best-effort now (see this
        module's own doc comment) -- record_placed/record_modified/record_cancelled are the
        primary write path.
        """
        for order in orders:
            order_id = order.get("gtt_order_id")
            if not isinstance(order_id, str) or not order_id:
                continue
            self._upsert(order_id, order.get("instrument_token"), order.get("status"), order)

    def record_placed(self, gtt_order_id: str, instrument_token: str, payload: dict[str, Any]) -> None:
        """Directly persists a just-placed GTT order the moment Upstox's place response confirms
        it -- independent of any later list call ever observing it. This is the actual fix: a
        resting order is now known here unconditionally, not only if Upstox's list happens to
        include it.
        """
        status = str(payload.get("status") or "ACTIVE").upper()
        self._upsert(gtt_order_id, instrument_token, status, {**payload, "status": status})

    def record_modified(self, gtt_order_id: str, instrument_token: str, payload: dict[str, Any]) -> None:
        """Updates a known order's stored payload directly after a successful modify -- no list
        round-trip needed, Upstox's modify response already confirmed the new rules.
        """
        status = str(payload.get("status") or "ACTIVE").upper()
        self._upsert(gtt_order_id, instrument_token, status, {**payload, "status": status})

    def record_cancelled(self, gtt_order_id: str) -> None:
        """Marks a known order CANCELLED directly after a successful cancel call -- again, no
        dependency on a later list call ever reporting the transition. A no-op if this id was
        never recorded here (e.g. a GTT placed before this backend tracked its own placements).
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM gtt_history WHERE gtt_order_id = ?", (gtt_order_id,)
            ).fetchone()
            if row is None:
                return
            payload = json.loads(row["payload"])
            payload["status"] = "CANCELLED"
            connection.execute(
                "UPDATE gtt_history SET status = 'CANCELLED', payload = ?, last_seen = CURRENT_TIMESTAMP "
                "WHERE gtt_order_id = ?",
                (json.dumps(payload), gtt_order_id),
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

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import Settings

# Same rationale as `journal_store._WEEKDAY_LABELS`: markets are closed Sat/Sun so those two
# will always come back zero, but they're kept in the list so `journal_analytics_summary`'s
# weekday_breakdown always renders a full, consistently-ordered 7-bar week.
_WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

"""Durable, server-side mirror of the new order engine's (`docs/ORDER_POSITION_OVERHAUL_DESIGN.md`
§8) `Lot`/`TriggerRule` ledger -- Part 4's own decision that the on-device Room DB stops being the
*only* record of an armed bracket's existence. Deliberately its own SQLite file/module, same
"new backend module, isolation rule" posture every other new piece of this overhaul has followed
(`order_engine_order_service.py`, `journal_store.py`'s own header comment) -- never touches
`JournalStore`'s tables or the old GTT-based order/position path.

This store is a *record*, not an evaluator: it never decides when to fire a trigger or place an
order (that's still the client's own tick-driven `TriggerEvaluator`, kept for latency, per §8.1) --
it exists so the server independently knows what's armed and what happened, durably, regardless of
whether any phone is currently connected.
"""


class OrderEngineLedgerStore:
    """`JournalStore`-shaped: plain `sqlite3`, own file, WAL, forward-only where practical.

    `lots`/`trigger_rules` are upserted (a client resend of the same id must not create a
    duplicate row -- the client is the one generating these UUIDs, this store just mirrors them).
    `order_engine_events` is genuinely append-only -- the audit trail that answers "what happened
    and when" independent of whatever the current row state says, deliberately never mutated or
    deleted by anything in this class.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.order_engine_ledger_database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lots (
                    id TEXT PRIMARY KEY,
                    instrument_key TEXT NOT NULL,
                    transaction_type TEXT NOT NULL,
                    entry_price REAL,
                    entry_quantity INTEGER NOT NULL,
                    remaining_quantity INTEGER NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    target_price REAL,
                    stoploss_price REAL,
                    trailing_gap REAL,
                    target_rule_id TEXT,
                    stoploss_rule_id TEXT,
                    product TEXT NOT NULL DEFAULT 'I',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    strategy_tag TEXT,
                    followed_plan INTEGER,
                    mistake_reason TEXT,
                    remarks TEXT,
                    confidence_score REAL,
                    setup_type TEXT
                );

                CREATE INDEX IF NOT EXISTS ix_lots_instrument
                    ON lots (instrument_key, state);

                CREATE TABLE IF NOT EXISTS trigger_rules (
                    id TEXT PRIMARY KEY,
                    lot_id TEXT REFERENCES lots(id),
                    instrument_key TEXT NOT NULL,
                    role TEXT,
                    state TEXT NOT NULL,
                    condition_op TEXT,
                    condition_value REAL,
                    sibling_rule_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_trigger_rules_lot
                    ON trigger_rules (lot_id);
                CREATE INDEX IF NOT EXISTS ix_trigger_rules_instrument
                    ON trigger_rules (instrument_key, state);

                CREATE TABLE IF NOT EXISTS order_engine_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lot_id TEXT,
                    rule_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT,
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_order_engine_events_lot
                    ON order_engine_events (lot_id, recorded_at);

                CREATE TABLE IF NOT EXISTS max_loss_epochs (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    opening_balance REAL NOT NULL,
                    peak_equity REAL NOT NULL,
                    threshold_x REAL NOT NULL,
                    threshold_mode TEXT NOT NULL DEFAULT 'ABSOLUTE',
                    epoch_started_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_history (
                    id TEXT PRIMARY KEY,
                    broker_order_id TEXT NOT NULL UNIQUE,
                    exchange_order_id TEXT,
                    idempotency_key TEXT,
                    order_tag TEXT,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT,
                    transaction_type TEXT NOT NULL,
                    product TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    requested_quantity INTEGER NOT NULL,
                    requested_price REAL,
                    trigger_price REAL,
                    status TEXT NOT NULL,
                    status_message TEXT,
                    average_price REAL,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    lot_id TEXT REFERENCES lots(id),
                    rule_id TEXT,
                    role TEXT,
                    placed_at TEXT,
                    last_broker_update_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    raw_broker_payload_json TEXT,
                    strategy_tag TEXT,
                    followed_plan INTEGER,
                    mistake_reason TEXT,
                    remarks TEXT,
                    confidence_score REAL,
                    setup_type TEXT,
                    charges REAL
                );

                CREATE INDEX IF NOT EXISTS ix_order_history_idempotency_key
                    ON order_history (idempotency_key);
                CREATE INDEX IF NOT EXISTS ix_order_history_lot
                    ON order_history (lot_id);
                CREATE INDEX IF NOT EXISTS ix_order_history_instrument_status
                    ON order_history (instrument_key, status);
                CREATE INDEX IF NOT EXISTS ix_order_history_created_at
                    ON order_history (created_at);
                """,
            )
            # `CREATE TABLE IF NOT EXISTS` above is a no-op against an already-existing `lots`
            # table from before `product` existed (this store's file persists across restarts,
            # under the same Docker-volume-backed directory every other *_path store in this app
            # uses) -- an explicit, idempotent ALTER is the only way an already-deployed table
            # actually gains the column. `duplicate column name` is SQLite's own error text for
            # "already migrated," swallowed the same way this repo's other one-shot ALTER-based
            # migrations do; any other OperationalError is a genuine problem and propagates.
            try:
                connection.execute("ALTER TABLE lots ADD COLUMN product TEXT NOT NULL DEFAULT 'I'")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
            # Journal v2 (docs/JOURNAL_V2 design session, 2026-08-16): a "trade" is a closed
            # `lots` row, so journaling annotations belong on `lots`, not `order_history` --
            # `order_history`'s own journaling columns (added in Part 5) stay reserved for the
            # rare `role='MANUAL'` row that has no lot at all. Same idempotent-ALTER pattern as
            # `product` above, one statement per column since SQLite's `ADD COLUMN` is
            # single-column-only.
            for column_sql in (
                "ALTER TABLE lots ADD COLUMN strategy_tag TEXT",
                "ALTER TABLE lots ADD COLUMN followed_plan INTEGER",
                "ALTER TABLE lots ADD COLUMN mistake_reason TEXT",
                "ALTER TABLE lots ADD COLUMN remarks TEXT",
                "ALTER TABLE lots ADD COLUMN confidence_score REAL",
                "ALTER TABLE lots ADD COLUMN setup_type TEXT",
                "ALTER TABLE order_history ADD COLUMN charges REAL",
            ):
                try:
                    connection.execute(column_sql)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- lots -----------------------------------------------------------------------------

    def upsert_lot(
        self,
        *,
        lot_id: str,
        instrument_key: str,
        transaction_type: str,
        entry_price: Optional[float],
        entry_quantity: int,
        remaining_quantity: int,
        realized_pnl: float,
        state: str,
        target_price: Optional[float] = None,
        stoploss_price: Optional[float] = None,
        trailing_gap: Optional[float] = None,
        target_rule_id: Optional[str] = None,
        stoploss_rule_id: Optional[str] = None,
        product: str = "I",
    ) -> dict[str, Any]:
        """Insert-or-update by `lot_id` -- the client generates this id, so a resend (retry after
        a dropped response, e.g.) upserts the same row rather than duplicating it.

        [product] closes half of `order_engine_max_loss_watcher.flatten_open_lots`'s own
        v1 scope note (`docs/ORDER_POSITION_OVERHAUL_DESIGN.md` §8.4 milestone 6): the ledger
        previously had no per-lot product recorded at all, so an emergency flatten always placed
        its exit at product `"I"` regardless of what the lot's own entry actually used. Defaults
        `"I"` so every pre-existing caller (and every already-persisted row, via this table's own
        `ALTER TABLE ... DEFAULT 'I'` migration) is unaffected."""
        now = self._now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM lots WHERE id = ?", (lot_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else now
            connection.execute(
                """
                INSERT INTO lots (
                    id, instrument_key, transaction_type, entry_price, entry_quantity,
                    remaining_quantity, realized_pnl, state, target_price, stoploss_price,
                    trailing_gap, target_rule_id, stoploss_rule_id, product, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    instrument_key = excluded.instrument_key,
                    transaction_type = excluded.transaction_type,
                    entry_price = excluded.entry_price,
                    entry_quantity = excluded.entry_quantity,
                    remaining_quantity = excluded.remaining_quantity,
                    realized_pnl = excluded.realized_pnl,
                    state = excluded.state,
                    target_price = excluded.target_price,
                    stoploss_price = excluded.stoploss_price,
                    trailing_gap = excluded.trailing_gap,
                    target_rule_id = excluded.target_rule_id,
                    stoploss_rule_id = excluded.stoploss_rule_id,
                    product = excluded.product,
                    updated_at = excluded.updated_at
                """,
                (
                    lot_id, instrument_key, transaction_type, entry_price, entry_quantity,
                    remaining_quantity, realized_pnl, state, target_price, stoploss_price,
                    trailing_gap, target_rule_id, stoploss_rule_id, product, created_at, now,
                ),
            )
        return self.get_lot(lot_id)  # type: ignore[return-value]

    def get_lot(self, lot_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_open_lots(self) -> list[dict[str, Any]]:
        """Every lot not in a terminal `CLOSED` state -- what `OrderEngineLotTracker`/
        `order_engine_max_loss_watcher.py` need to know what to watch."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM lots WHERE state != 'CLOSED' ORDER BY created_at",
            ).fetchall()
        return [dict(row) for row in rows]

    def get_open_lots_for_instrument(self, instrument_key: str) -> list[dict[str, Any]]:
        """Same as [get_open_lots] but scoped to one instrument -- what
        `OrderEngineLotTracker.apply_tick` needs on every live tick without re-scanning every
        open lot across every instrument on each call."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM lots WHERE state != 'CLOSED' AND instrument_key = ? ORDER BY created_at",
                (instrument_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- trigger rules ----------------------------------------------------------------------

    def upsert_trigger_rule(
        self,
        *,
        rule_id: str,
        lot_id: Optional[str],
        instrument_key: str,
        role: Optional[str],
        state: str,
        condition_op: Optional[str],
        condition_value: Optional[float],
        sibling_rule_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = self._now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM trigger_rules WHERE id = ?", (rule_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else now
            connection.execute(
                """
                INSERT INTO trigger_rules (
                    id, lot_id, instrument_key, role, state, condition_op, condition_value,
                    sibling_rule_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    lot_id = excluded.lot_id,
                    instrument_key = excluded.instrument_key,
                    role = excluded.role,
                    state = excluded.state,
                    condition_op = excluded.condition_op,
                    condition_value = excluded.condition_value,
                    sibling_rule_id = excluded.sibling_rule_id,
                    updated_at = excluded.updated_at
                """,
                (
                    rule_id, lot_id, instrument_key, role, state, condition_op, condition_value,
                    sibling_rule_id, created_at, now,
                ),
            )
        return self.get_trigger_rule(rule_id)  # type: ignore[return-value]

    def get_trigger_rule(self, rule_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM trigger_rules WHERE id = ?", (rule_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_armed_trigger_rules(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM trigger_rules WHERE state = 'ARMED' ORDER BY created_at",
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trigger_rules_by_state(self, state: str) -> list[dict[str, Any]]:
        """General-purpose sibling to [get_armed_trigger_rules] -- needed by
        `_on_portfolio_update`'s exit-reconciliation wiring, which has to scan every currently
        `PLACED` bracket leg to find the one whose derived Upstox tag matches an incoming
        `order_update`'s `tag` (see `order_engine_order_service.derive_order_tag` -- the mapping
        is one-way, so there's no direct tag -> rule_id lookup, only this kind of scan)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM trigger_rules WHERE state = ? ORDER BY created_at", (state,),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- events -----------------------------------------------------------------------------

    def record_event(
        self,
        *,
        event_type: str,
        lot_id: Optional[str] = None,
        rule_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append-only -- never updated or deleted. `event_type` is a free-form string
        (`ARMED`/`FIRED`/`CANCELLED`/`FILLED`/`ESCALATED`/... -- whatever the caller names it),
        deliberately not an enum here since the server shouldn't need a matching code change every
        time the client-side state machine grows a new transition worth recording."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO order_engine_events (lot_id, rule_id, event_type, payload_json, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    lot_id, rule_id, event_type,
                    json.dumps(payload or {}, separators=(",", ":"), sort_keys=True, default=str),
                    self._now(),
                ),
            )

    def get_all_lots(self) -> list[dict[str, Any]]:
        """Every lot regardless of state -- unlike [get_open_lots], this includes `CLOSED` lots
        too, needed to sum whole-day realized P&L the same way the client's own
        `PnLCalculator.realizedPnl` does (every lot's own banked `realized_pnl`, closed or not)."""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM lots ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    # -- max-loss epoch (§7.10-equivalent, server-side; §8.3 "the server cannot default") --------

    def upsert_max_loss_epoch(
        self,
        *,
        opening_balance: float,
        peak_equity: float,
        threshold_x: float,
        epoch_started_at: str,
        threshold_mode: str = "ABSOLUTE",
    ) -> dict[str, Any]:
        """Single-row table (mirrors the client's own `MaxLossEpochDao`/`MaxLossEpoch.SINGLETON_ID`
        convention) -- there is only ever one active epoch at a time, replaced wholesale on
        `startEpoch`/auto-re-arm, never accumulated as a history here.

        [threshold_mode] is `"ABSOLUTE"` (X is a fixed currency amount, the original §7.10
        behavior) or `"PERCENTAGE"` (X is a percentage of the breach formula's own reference
        point -- see §7.10's 2026-08-12 amendment and `order_engine_max_loss_watcher._effective_threshold`
        for where this actually gets interpreted; this store just persists the caller's choice
        verbatim, same "mechanism, not policy" separation the rest of this package uses)."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO max_loss_epochs (id, opening_balance, peak_equity, threshold_x, threshold_mode, epoch_started_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    opening_balance = excluded.opening_balance,
                    peak_equity = excluded.peak_equity,
                    threshold_x = excluded.threshold_x,
                    threshold_mode = excluded.threshold_mode,
                    epoch_started_at = excluded.epoch_started_at
                """,
                (opening_balance, peak_equity, threshold_x, threshold_mode, epoch_started_at),
            )
        return self.get_max_loss_epoch()  # type: ignore[return-value]

    def get_max_loss_epoch(self) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM max_loss_epochs WHERE id = 1").fetchone()
        return dict(row) if row is not None else None

    def get_events_for_lot(self, lot_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM order_engine_events WHERE lot_id = ? ORDER BY recorded_at, id",
                (lot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- order history (Part 5, docs/ORDER_HISTORY_V2_DESIGN.md) ----------------------------

    def upsert_order(
        self,
        *,
        id: str,  # noqa: A002 - matches the row's own primary key name, not the builtin shadow risk here
        broker_order_id: str,
        exchange_order_id: Optional[str],
        idempotency_key: Optional[str],
        order_tag: Optional[str],
        instrument_key: str,
        trading_symbol: Optional[str],
        transaction_type: str,
        product: str,
        order_type: str,
        requested_quantity: int,
        requested_price: Optional[float],
        trigger_price: Optional[float],
        status: str,
        status_message: Optional[str],
        average_price: Optional[float],
        filled_quantity: int,
        lot_id: Optional[str] = None,
        rule_id: Optional[str] = None,
        role: Optional[str] = None,
        placed_at: Optional[str] = None,
        last_broker_update_at: Optional[str] = None,
        raw_broker_payload_json: Optional[str] = None,
    ) -> dict[str, Any]:
        """Insert-or-update by `broker_order_id` -- one row per real broker order, never
        averaged/merged across separate orders. A repeat sighting of the same order (a status
        transition, a later fill) updates the same row in place rather than duplicating it.
        Journaling columns (`strategy_tag`/`followed_plan`/`mistake_reason`/`remarks`/
        `confidence_score`/`setup_type`) are deliberately never written here -- left `NULL` for a
        future journal v2 UI/endpoint to fill in, per `docs/ORDER_HISTORY_V2_DESIGN.md`."""
        now = self._now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id, created_at FROM order_history WHERE broker_order_id = ?",
                (broker_order_id,),
            ).fetchone()
            row_id = existing["id"] if existing is not None else id
            created_at = existing["created_at"] if existing is not None else now
            connection.execute(
                """
                INSERT INTO order_history (
                    id, broker_order_id, exchange_order_id, idempotency_key, order_tag,
                    instrument_key, trading_symbol, transaction_type, product, order_type,
                    requested_quantity, requested_price, trigger_price, status, status_message,
                    average_price, filled_quantity, lot_id, rule_id, role, placed_at,
                    last_broker_update_at, created_at, updated_at, raw_broker_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(broker_order_id) DO UPDATE SET
                    exchange_order_id = excluded.exchange_order_id,
                    idempotency_key = COALESCE(excluded.idempotency_key, order_history.idempotency_key),
                    order_tag = COALESCE(excluded.order_tag, order_history.order_tag),
                    trading_symbol = excluded.trading_symbol,
                    status = excluded.status,
                    status_message = excluded.status_message,
                    average_price = excluded.average_price,
                    filled_quantity = excluded.filled_quantity,
                    lot_id = COALESCE(excluded.lot_id, order_history.lot_id),
                    rule_id = COALESCE(excluded.rule_id, order_history.rule_id),
                    role = COALESCE(excluded.role, order_history.role),
                    last_broker_update_at = excluded.last_broker_update_at,
                    updated_at = excluded.updated_at,
                    raw_broker_payload_json = excluded.raw_broker_payload_json
                """,
                (
                    row_id, broker_order_id, exchange_order_id, idempotency_key, order_tag,
                    instrument_key, trading_symbol, transaction_type, product, order_type,
                    requested_quantity, requested_price, trigger_price, status, status_message,
                    average_price, filled_quantity, lot_id, rule_id, role, placed_at,
                    last_broker_update_at, created_at, now, raw_broker_payload_json,
                ),
            )
        return self.get_order_by_broker_order_id(broker_order_id)  # type: ignore[return-value]

    def get_order_by_broker_order_id(self, broker_order_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM order_history WHERE broker_order_id = ?", (broker_order_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_orders(
        self,
        *,
        limit: int = 50,
        before: Optional[str] = None,
        instrument_key: Optional[str] = None,
        status: Optional[str] = None,
        lot_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Cursor-paginated, newest first (`created_at DESC, id DESC`) -- avoids offset drift on
        this append-heavy, unbounded table. [before] is a previous page's last row `id`; the
        cursor resolves to that row's own `(created_at, id)` and the next page is everything
        strictly older than it, so concurrent inserts at the head never shift an in-progress
        page. Backs `GET /order-engine/orders`."""
        clauses: list[str] = []
        params: list[Any] = []
        if before is not None:
            anchor = None
            with self._connect() as connection:
                anchor = connection.execute(
                    "SELECT created_at, id FROM order_history WHERE id = ?", (before,),
                ).fetchone()
            if anchor is not None:
                clauses.append("(created_at, id) < (?, ?)")
                params.extend([anchor["created_at"], anchor["id"]])
        if instrument_key is not None:
            clauses.append("instrument_key = ?")
            params.append(instrument_key)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if lot_id is not None:
            clauses.append("lot_id = ?")
            params.append(lot_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM order_history
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- journal v2 (design session 2026-08-16) --------------------------------------------
    #
    # A "trade" here is a closed `lots` row -- entry/exit averaging and realized P&L are already
    # computed by `OrderHistoryRecorder`/`compute_realized_pnl`, so this layer is pure read-side
    # aggregation, not a fill-matcher (unlike v1's `JournalStore.rebuild_session`). Manual trades
    # (no lot workflow) are `order_history` rows with `role='MANUAL'` and no `lot_id`.

    _NOTES_COLUMNS = (
        "strategy_tag", "followed_plan", "mistake_reason", "remarks", "confidence_score",
        "setup_type",
    )

    def list_closed_lots(
        self, *, limit: int = 50, before: Optional[str] = None,
        instrument_key: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Cursor-paginated (`updated_at DESC, id DESC`) closed lots -- a lot's `updated_at` is
        only bumped again once, at close, since `OrderHistoryRecorder` never re-upserts a
        `CLOSED` lot, so it doubles as "closed_at" without a dedicated column."""
        clauses = ["state = 'CLOSED'"]
        params: list[Any] = []
        if before is not None:
            with self._connect() as connection:
                anchor = connection.execute(
                    "SELECT updated_at, id FROM lots WHERE id = ?", (before,),
                ).fetchone()
            if anchor is not None:
                clauses.append("(updated_at, id) < (?, ?)")
                params.extend([anchor["updated_at"], anchor["id"]])
        if instrument_key is not None:
            clauses.append("instrument_key = ?")
            params.append(instrument_key)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM lots WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_orders_for_lot(self, lot_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM order_history WHERE lot_id = ? ORDER BY created_at, id",
                (lot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_lot_notes(self, lot_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        """Journaling-only update -- never touches any position/P&L column. `fields` keys must
        be a subset of `_NOTES_COLUMNS`; unset keys are left as-is (partial update)."""
        updates = {key: value for key, value in fields.items() if key in self._NOTES_COLUMNS}
        if not updates:
            return self.get_lot(lot_id)
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE lots SET {set_clause}, updated_at = ? WHERE id = ?",
                (*updates.values(), self._now(), lot_id),
            )
        return self.get_lot(lot_id)

    def update_order_notes(self, order_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        """Same as [update_lot_notes] but for a `role='MANUAL'` `order_history` row that has no
        lot at all -- `order_id` is `order_history.id` (the app's own UUID), not
        `broker_order_id`."""
        updates = {key: value for key, value in fields.items() if key in self._NOTES_COLUMNS}
        if not updates:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM order_history WHERE id = ?", (order_id,),
                ).fetchone()
            return dict(row) if row is not None else None
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE order_history SET {set_clause}, updated_at = ? WHERE id = ?",
                (*updates.values(), self._now(), order_id),
            )
            row = connection.execute(
                "SELECT * FROM order_history WHERE id = ?", (order_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def create_manual_order(
        self,
        *,
        order_id: str,
        instrument_key: str,
        trading_symbol: Optional[str],
        transaction_type: str,
        product: str,
        quantity: int,
        average_price: Optional[float],
        placed_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """A manually-entered trade with no real broker order behind it -- same `role='MANUAL'`
        convention `OrderHistoryRecorder` already uses for unmatched real orders, so both kinds
        of "no lot workflow" row share one code path everywhere downstream (list/detail/notes).
        `broker_order_id` gets a synthetic `manual:{order_id}` value since the column is
        `NOT NULL UNIQUE` and a manual entry has no real one."""
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO order_history (
                    id, broker_order_id, instrument_key, trading_symbol, transaction_type,
                    product, order_type, requested_quantity, status, average_price,
                    filled_quantity, role, placed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'MARKET', ?, 'complete', ?, ?, 'MANUAL', ?, ?, ?)
                """,
                (
                    order_id, f"manual:{order_id}", instrument_key, trading_symbol,
                    transaction_type, product, quantity, average_price, quantity,
                    placed_at or now, now, now,
                ),
            )
        return self.get_order_by_broker_order_id(f"manual:{order_id}")  # type: ignore[return-value]

    def journal_filter_options(self) -> dict[str, list[str]]:
        with self._connect() as connection:
            symbols = [
                row[0] for row in connection.execute(
                    "SELECT DISTINCT trading_symbol FROM order_history "
                    "WHERE trading_symbol IS NOT NULL ORDER BY trading_symbol",
                ) if row[0]
            ]
            setups = [
                row[0] for row in connection.execute(
                    "SELECT DISTINCT setup_type FROM lots WHERE setup_type IS NOT NULL "
                    "UNION SELECT DISTINCT setup_type FROM order_history "
                    "WHERE setup_type IS NOT NULL ORDER BY 1",
                ) if row[0]
            ]
            strategy_tags = [
                row[0] for row in connection.execute(
                    "SELECT DISTINCT strategy_tag FROM lots WHERE strategy_tag IS NOT NULL "
                    "UNION SELECT DISTINCT strategy_tag FROM order_history "
                    "WHERE strategy_tag IS NOT NULL ORDER BY 1",
                ) if row[0]
            ]
        return {
            "trading_symbols": symbols, "setups": setups, "strategy_tags": strategy_tags,
        }

    def journal_analytics_summary(
        self, *, start_date: Optional[str] = None, end_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """Same MVP scope/shape as v1's `JournalStore.analytics_summary` (summary KPIs + equity
        curve + weekday breakdown, always-7-days, `n<30` low-sample flag), computed off closed
        `lots.realized_pnl` instead of `journal_trades`. **`net_pnl` is intentionally `None`
        here** -- per-order/day-level charges aren't computed yet (design session 2026-08-16
        decision: reserve the `order_history.charges` column, don't build the opening-balance-
        diff mechanism until the exact Upstox charge formula is confirmed). Every figure below is
        gross, not net, until that lands."""
        filters = ["state = 'CLOSED'"]
        values: list[Any] = []
        if start_date:
            filters.append("date(updated_at) >= ?")
            values.append(start_date)
        if end_date:
            filters.append("date(updated_at) <= ?")
            values.append(end_date)
        where = " AND ".join(filters)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT realized_pnl, updated_at FROM lots WHERE {where} ORDER BY updated_at, id",
                values,
            ).fetchall()
        pnls = [float(row["realized_pnl"]) for row in rows]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        equity: list[float] = []
        running = 0.0
        for value in pnls:
            running += value
            equity.append(running)
        weekday: dict[str, list[float]] = {day: [] for day in _WEEKDAY_LABELS}
        for row in rows:
            label = datetime.fromisoformat(row["updated_at"]).strftime("%A")
            weekday.setdefault(label, []).append(float(row["realized_pnl"]))
        return {
            "trade_count": len(pnls),
            "gross_pnl": sum(pnls),
            "net_pnl": None,
            "win_rate": len(wins) / len(pnls) * 100 if pnls else 0.0,
            "average_win": sum(wins) / len(wins) if wins else 0.0,
            "average_loss": sum(losses) / len(losses) if losses else 0.0,
            "best_trade": max(pnls) if pnls else 0.0,
            "worst_trade": min(pnls) if pnls else 0.0,
            "equity_curve": equity,
            "low_sample": len(pnls) < 30,
            "weekday_breakdown": [
                {
                    "label": label,
                    "trade_count": len(day_values),
                    "net_pnl": sum(day_values),
                    "win_rate": (
                        sum(1 for value in day_values if value > 0) / len(day_values) * 100
                        if day_values else 0.0
                    ),
                    "low_sample": len(day_values) < 30,
                }
                for label in _WEEKDAY_LABELS
                for day_values in [weekday[label]]
            ],
        }

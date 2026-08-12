from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import Settings

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
                    updated_at TEXT NOT NULL
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

from __future__ import annotations

import json
import hashlib
import uuid
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import Settings


class JournalStore:
    """Durable, forward-only journal storage.

    Market context is intentionally independent from fill matching: it starts accumulating as
    soon as Phase 1 is deployed, even while the ledger and journal UI are still absent.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.journal_database_path)
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
                CREATE TABLE IF NOT EXISTS journal_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trade_context (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    underlying_key TEXT NOT NULL,
                    expiry_date TEXT,
                    contract_ltp REAL,
                    captured_at TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    UNIQUE(order_id, trigger)
                );

                CREATE INDEX IF NOT EXISTS ix_trade_context_captured_at
                    ON trade_context (captured_at);
                CREATE INDEX IF NOT EXISTS ix_trade_context_instrument
                    ON trade_context (instrument_key);

                CREATE TABLE IF NOT EXISTS journal_sessions (
                    trading_date TEXT PRIMARY KEY,
                    first_sync_at TEXT NOT NULL,
                    last_sync_at TEXT NOT NULL,
                    close_reconciled_at TEXT,
                    status TEXT NOT NULL DEFAULT 'partial',
                    gap_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS trade_fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    transaction_type TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    executed_at TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    exchange TEXT,
                    segment TEXT,
                    option_type TEXT,
                    strike_price TEXT,
                    expiry TEXT,
                    computed_charges REAL NOT NULL DEFAULT 0,
                    raw_payload_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_trade_fills_session_instrument
                    ON trade_fills (trade_date, instrument_key, executed_at, fill_id);

                CREATE TABLE IF NOT EXISTS journal_trades (
                    id TEXT PRIMARY KEY,
                    match_fingerprint TEXT UNIQUE NOT NULL,
                    instrument_key TEXT NOT NULL,
                    trading_symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    gross_pnl REAL NOT NULL,
                    computed_charges REAL NOT NULL DEFAULT 0,
                    manual_charge_override REAL,
                    status TEXT NOT NULL DEFAULT 'closed',
                    source TEXT NOT NULL DEFAULT 'automatic',
                    excluded INTEGER NOT NULL DEFAULT 0,
                    calculation_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_journal_trades_closed
                    ON journal_trades (closed_at DESC);
                CREATE INDEX IF NOT EXISTS ix_journal_trades_date
                    ON journal_trades (trade_date);

                CREATE TABLE IF NOT EXISTS journal_trade_fills (
                    journal_trade_id TEXT NOT NULL REFERENCES journal_trades(id),
                    fill_id TEXT NOT NULL REFERENCES trade_fills(fill_id),
                    role TEXT NOT NULL,
                    allocated_quantity REAL NOT NULL,
                    PRIMARY KEY (journal_trade_id, fill_id, role)
                );

                CREATE TABLE IF NOT EXISTS journal_notes (
                    journal_trade_id TEXT PRIMARY KEY REFERENCES journal_trades(id),
                    setup TEXT,
                    entry_reason TEXT,
                    exit_reason TEXT,
                    plan TEXT,
                    mistakes TEXT,
                    lessons TEXT,
                    notes TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    confidence_rating INTEGER,
                    execution_rating INTEGER,
                    reviewed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO journal_metadata (key, value)
                VALUES ('ledger_started_at', ?)
                """,
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
            )

    def record_context(
        self,
        *,
        order_id: str,
        trigger: str,
        instrument_key: str,
        underlying_key: str,
        expiry_date: Optional[str],
        contract_ltp: Optional[float],
        context: Optional[dict[str, Any]],
        captured_at: Optional[datetime] = None,
    ) -> bool:
        """Insert one context snapshot, returning False for a duplicate order/trigger."""
        moment = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        context_json = json.dumps(
            context or {},
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO trade_context (
                    order_id, trigger, instrument_key, underlying_key, expiry_date,
                    contract_ltp, captured_at, context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    trigger,
                    instrument_key,
                    underlying_key,
                    expiry_date,
                    contract_ltp,
                    moment.isoformat(timespec="seconds"),
                    context_json,
                ),
            )
        return cursor.rowcount > 0

    def get_context(self, order_id: str, trigger: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT order_id, trigger, instrument_key, underlying_key, expiry_date,
                       contract_ltp, captured_at, context_json
                FROM trade_context
                WHERE order_id = ? AND trigger = ?
                """,
                (order_id, trigger),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["context"] = json.loads(result.pop("context_json"))
        return result

    def upsert_fill(self, fill: dict[str, Any]) -> bool:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        raw = json.dumps(fill.get("raw_payload", fill), separators=(",", ":"), sort_keys=True, default=str)
        with self._connect() as connection:
            existed = connection.execute(
                "SELECT 1 FROM trade_fills WHERE fill_id = ?", (fill["fill_id"],),
            ).fetchone() is not None
            connection.execute(
                """
                INSERT INTO trade_fills (
                    fill_id, order_id, instrument_key, trading_symbol, transaction_type,
                    quantity, price, executed_at, trade_date, exchange, segment, option_type,
                    strike_price, expiry, computed_charges, raw_payload_json, first_seen_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fill_id) DO UPDATE SET
                    order_id=excluded.order_id,
                    instrument_key=excluded.instrument_key,
                    trading_symbol=excluded.trading_symbol,
                    transaction_type=excluded.transaction_type,
                    quantity=excluded.quantity,
                    price=excluded.price,
                    executed_at=excluded.executed_at,
                    trade_date=excluded.trade_date,
                    computed_charges=excluded.computed_charges,
                    raw_payload_json=excluded.raw_payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    fill["fill_id"], fill["order_id"], fill["instrument_key"],
                    fill.get("trading_symbol", ""), fill["transaction_type"], fill["quantity"],
                    fill["price"], fill["executed_at"], fill["trade_date"], fill.get("exchange"),
                    fill.get("segment"), fill.get("option_type"), fill.get("strike_price"),
                    fill.get("expiry"), fill.get("computed_charges", 0.0), raw, now, now,
                ),
            )
        return not existed

    def record_session_sync(self, trading_date: str, *, complete: bool = False) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO journal_sessions (
                    trading_date, first_sync_at, last_sync_at, close_reconciled_at, status
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trading_date) DO UPDATE SET
                    last_sync_at=excluded.last_sync_at,
                    close_reconciled_at=COALESCE(excluded.close_reconciled_at, close_reconciled_at),
                    status=CASE WHEN excluded.status='complete' THEN 'complete' ELSE status END
                """,
                (trading_date, now, now, now if complete else None, "complete" if complete else "partial"),
            )

    def fills_for_session(self, trading_date: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM trade_fills WHERE trade_date = ?
                ORDER BY instrument_key, executed_at, fill_id
                """,
                (trading_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def rebuild_session(self, trading_date: str) -> list[str]:
        """FIFO-match a session. Annotated trades whose fill set changes are protected."""
        fills = self.fills_for_session(trading_date)
        candidates: list[dict[str, Any]] = []
        by_instrument: dict[str, list[dict[str, Any]]] = {}
        for fill in fills:
            by_instrument.setdefault(fill["instrument_key"], []).append(fill)
        for instrument_fills in by_instrument.values():
            candidates.extend(_match_round_trips(instrument_fills))

        fingerprints = {candidate["match_fingerprint"] for candidate in candidates}
        conflicts: list[str] = []
        blocked_fingerprints: set[str] = set()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT t.id, t.match_fingerprint,
                       EXISTS(SELECT 1 FROM journal_notes n WHERE n.journal_trade_id=t.id) annotated
                FROM journal_trades t WHERE t.trade_date=? AND t.source='automatic'
                """,
                (trading_date,),
            ).fetchall()
            for row in existing:
                if row["match_fingerprint"] in fingerprints:
                    continue
                if row["annotated"]:
                    protected_fill_ids = {
                        fill_row[0] for fill_row in connection.execute(
                            "SELECT fill_id FROM journal_trade_fills WHERE journal_trade_id=?",
                            (row["id"],),
                        )
                    }
                    for candidate in candidates:
                        candidate_fill_ids = {
                            allocation["fill_id"] for allocation in candidate["allocations"]
                        }
                        if protected_fill_ids & candidate_fill_ids:
                            blocked_fingerprints.add(candidate["match_fingerprint"])
                    connection.execute(
                        "UPDATE journal_trades SET status='needs_review', updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    conflicts.append(row["id"])
                else:
                    connection.execute("DELETE FROM journal_trade_fills WHERE journal_trade_id=?", (row["id"],))
                    connection.execute("DELETE FROM journal_trades WHERE id=?", (row["id"],))

            for candidate in candidates:
                if candidate["match_fingerprint"] in blocked_fingerprints:
                    continue
                trade_id = candidate["match_fingerprint"]
                connection.execute(
                    """
                    INSERT INTO journal_trades (
                        id, match_fingerprint, instrument_key, trading_symbol, trade_date,
                        direction, quantity, entry_price, exit_price, opened_at, closed_at,
                        gross_pnl, computed_charges, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?)
                    ON CONFLICT(match_fingerprint) DO UPDATE SET
                        computed_charges=excluded.computed_charges,
                        gross_pnl=excluded.gross_pnl,
                        updated_at=excluded.updated_at
                    """,
                    (
                        trade_id, candidate["match_fingerprint"], candidate["instrument_key"],
                        candidate["trading_symbol"], trading_date, candidate["direction"],
                        candidate["quantity"], candidate["entry_price"], candidate["exit_price"],
                        candidate["opened_at"], candidate["closed_at"], candidate["gross_pnl"],
                        candidate["computed_charges"], now, now,
                    ),
                )
                connection.execute("DELETE FROM journal_trade_fills WHERE journal_trade_id=?", (trade_id,))
                connection.executemany(
                    """
                    INSERT INTO journal_trade_fills (
                        journal_trade_id, fill_id, role, allocated_quantity
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (trade_id, item["fill_id"], item["role"], item["quantity"])
                        for item in candidate["allocations"]
                    ],
                )
        return conflicts

    def list_trades(
        self, *, page_number: int = 1, page_size: int = 20,
        start_date: Optional[str] = None, end_date: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        filters = ["t.excluded = 0"]
        values: list[Any] = []
        if start_date:
            filters.append("t.trade_date >= ?")
            values.append(start_date)
        if end_date:
            filters.append("t.trade_date <= ?")
            values.append(end_date)
        where = " AND ".join(filters)
        with self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM journal_trades t WHERE {where}", values,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT t.*, n.setup, n.tags_json, n.reviewed
                FROM journal_trades t
                LEFT JOIN journal_notes n ON n.journal_trade_id=t.id
                WHERE {where}
                ORDER BY t.closed_at DESC, t.id DESC LIMIT ? OFFSET ?
                """,
                [*values, page_size, (page_number - 1) * page_size],
            ).fetchall()
        items = [_trade_row(row) for row in rows]
        return items, {
            "page_number": page_number, "page_size": page_size, "total_records": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
        }

    def get_trade(self, trade_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT t.*, n.setup, n.entry_reason, n.exit_reason, n.plan, n.mistakes,
                       n.lessons, n.notes, n.tags_json, n.confidence_rating,
                       n.execution_rating, n.reviewed, n.updated_at notes_updated_at
                FROM journal_trades t LEFT JOIN journal_notes n ON n.journal_trade_id=t.id
                WHERE t.id=?
                """,
                (trade_id,),
            ).fetchone()
            fills = connection.execute(
                """
                SELECT f.*, l.role, l.allocated_quantity
                FROM journal_trade_fills l JOIN trade_fills f ON f.fill_id=l.fill_id
                WHERE l.journal_trade_id=? ORDER BY f.executed_at, f.fill_id
                """,
                (trade_id,),
            ).fetchall()
        if row is None:
            return None
        result = _trade_row(row)
        result["fills"] = [dict(fill) for fill in fills]
        return result

    def save_notes(self, trade_id: str, notes: dict[str, Any]) -> Optional[dict[str, Any]]:
        if self.get_trade(trade_id) is None:
            return None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO journal_notes (
                    journal_trade_id, setup, entry_reason, exit_reason, plan, mistakes, lessons,
                    notes, tags_json, confidence_rating, execution_rating, reviewed, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(journal_trade_id) DO UPDATE SET
                    setup=excluded.setup, entry_reason=excluded.entry_reason,
                    exit_reason=excluded.exit_reason, plan=excluded.plan,
                    mistakes=excluded.mistakes, lessons=excluded.lessons, notes=excluded.notes,
                    tags_json=excluded.tags_json, confidence_rating=excluded.confidence_rating,
                    execution_rating=excluded.execution_rating, reviewed=excluded.reviewed,
                    updated_at=excluded.updated_at
                """,
                (
                    trade_id, notes.get("setup"), notes.get("entry_reason"), notes.get("exit_reason"),
                    notes.get("plan"), notes.get("mistakes"), notes.get("lessons"), notes.get("notes"),
                    json.dumps(notes.get("tags", []), separators=(",", ":")),
                    notes.get("confidence_rating"), notes.get("execution_rating"),
                    int(bool(notes.get("reviewed"))), now,
                ),
            )
        return self.get_trade(trade_id)

    def create_manual_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        trade_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fingerprint = f"manual:{trade_id}"
        gross = float(trade["gross_pnl"])
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO journal_trades (
                    id, match_fingerprint, instrument_key, trading_symbol, trade_date,
                    direction, quantity, entry_price, exit_price, opened_at, closed_at,
                    gross_pnl, computed_charges, manual_charge_override, status, source,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'closed', 'manual', ?, ?)
                """,
                (
                    trade_id, fingerprint, trade["instrument_key"], trade["trading_symbol"],
                    trade["trade_date"], trade["direction"], trade["quantity"],
                    trade["entry_price"], trade["exit_price"], trade["opened_at"],
                    trade["closed_at"], gross, trade.get("charges", 0.0), now, now,
                ),
            )
        if trade.get("journal"):
            self.save_notes(trade_id, trade["journal"])
        result = self.get_trade(trade_id)
        assert result is not None
        return result

    def filter_options(self) -> dict[str, list[str]]:
        with self._connect() as connection:
            symbols = [
                row[0] for row in connection.execute(
                    "SELECT DISTINCT trading_symbol FROM journal_trades ORDER BY trading_symbol",
                ) if row[0]
            ]
            setups = [
                row[0] for row in connection.execute(
                    "SELECT DISTINCT setup FROM journal_notes WHERE setup IS NOT NULL ORDER BY setup",
                ) if row[0]
            ]
            tag_rows = connection.execute("SELECT tags_json FROM journal_notes").fetchall()
        tags = sorted({
            tag for row in tag_rows for tag in json.loads(row[0] or "[]") if isinstance(tag, str)
        })
        return {"trading_symbols": symbols, "setups": setups, "tags": tags}

    def analytics_summary(
        self, *, start_date: Optional[str] = None, end_date: Optional[str] = None,
    ) -> dict[str, Any]:
        filters = ["excluded=0", "status='closed'"]
        values: list[Any] = []
        if start_date:
            filters.append("trade_date>=?")
            values.append(start_date)
        if end_date:
            filters.append("trade_date<=?")
            values.append(end_date)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT gross_pnl,
                       gross_pnl - COALESCE(manual_charge_override, computed_charges) net_pnl
                FROM journal_trades WHERE {' AND '.join(filters)}
                ORDER BY closed_at, id
                """,
                values,
            ).fetchall()
        nets = [float(row["net_pnl"]) for row in rows]
        wins = [value for value in nets if value > 0]
        losses = [value for value in nets if value < 0]
        equity = []
        running = 0.0
        for value in nets:
            running += value
            equity.append(running)
        weekday: dict[str, list[float]] = {}
        with self._connect() as connection:
            dated_rows = connection.execute(
                f"""
                SELECT trade_date,
                       gross_pnl - COALESCE(manual_charge_override, computed_charges) net_pnl
                FROM journal_trades WHERE {' AND '.join(filters)}
                """,
                values,
            ).fetchall()
        for row in dated_rows:
            label = date.fromisoformat(row["trade_date"]).strftime("%A")
            weekday.setdefault(label, []).append(float(row["net_pnl"]))
        return {
            "trade_count": len(nets),
            "gross_pnl": sum(float(row["gross_pnl"]) for row in rows),
            "net_pnl": sum(nets),
            "win_rate": len(wins) / len(nets) * 100 if nets else 0.0,
            "average_win": sum(wins) / len(wins) if wins else 0.0,
            "average_loss": sum(losses) / len(losses) if losses else 0.0,
            "best_trade": max(nets) if nets else 0.0,
            "worst_trade": min(nets) if nets else 0.0,
            "equity_curve": equity,
            "low_sample": len(nets) < 30,
            "weekday_breakdown": [
                {
                    "label": label,
                    "trade_count": len(day_values),
                    "net_pnl": sum(day_values),
                    "win_rate": (
                        sum(1 for value in day_values if value > 0) / len(day_values) * 100
                    ),
                    "low_sample": len(day_values) < 30,
                }
                for label, day_values in weekday.items()
            ],
        }


def _trade_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["net_pnl"] = result["gross_pnl"] - (
        result["manual_charge_override"]
        if result["manual_charge_override"] is not None else result["computed_charges"]
    )
    result["excluded"] = bool(result["excluded"])
    if "reviewed" in result:
        result["reviewed"] = bool(result.get("reviewed"))
    if "tags_json" in result:
        result["tags"] = json.loads(result.pop("tags_json") or "[]")
    return result


def _match_round_trips(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match flat-to-flat sequences; reversal sequences are intentionally left unmatched."""
    results: list[dict[str, Any]] = []
    sequence: list[dict[str, Any]] = []
    exposure = 0.0
    for fill in fills:
        signed = fill["quantity"] if fill["transaction_type"].upper() == "BUY" else -fill["quantity"]
        if exposure and exposure * (exposure + signed) < 0:
            return results
        sequence.append(fill)
        exposure += signed
        if abs(exposure) > 1e-9:
            continue
        first_side = sequence[0]["transaction_type"].upper()
        entries = [item for item in sequence if item["transaction_type"].upper() == first_side]
        exits = [item for item in sequence if item["transaction_type"].upper() != first_side]
        quantity = sum(float(item["quantity"]) for item in entries)
        if quantity <= 0 or not exits:
            sequence = []
            continue
        entry_price = sum(float(item["quantity"]) * float(item["price"]) for item in entries) / quantity
        exit_quantity = sum(float(item["quantity"]) for item in exits)
        exit_price = sum(float(item["quantity"]) * float(item["price"]) for item in exits) / exit_quantity
        direction = "long" if first_side == "BUY" else "short"
        gross = (exit_price - entry_price) * quantity * (1 if direction == "long" else -1)
        allocations = [
            {
                "fill_id": item["fill_id"],
                "role": "entry" if item["transaction_type"].upper() == first_side else "exit",
                "quantity": item["quantity"],
            }
            for item in sequence
        ]
        fingerprint_source = "|".join(
            sorted(f"{item['fill_id']}:{item['role']}:{item['quantity']}" for item in allocations)
        )
        fingerprint = hashlib.sha256(fingerprint_source.encode()).hexdigest()
        results.append({
            "match_fingerprint": fingerprint,
            "instrument_key": sequence[0]["instrument_key"],
            "trading_symbol": sequence[0]["trading_symbol"],
            "direction": direction,
            "quantity": quantity,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "opened_at": sequence[0]["executed_at"],
            "closed_at": sequence[-1]["executed_at"],
            "gross_pnl": gross,
            "computed_charges": sum(float(item["computed_charges"]) for item in sequence),
            "allocations": allocations,
        })
        sequence = []
    return results

    def latest_placement_context(self, instrument_key: str) -> Optional[dict[str, Any]]:
        """Most recent placement for a contract, used when a GTT fill gets a new broker order ID."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT order_id, trigger, instrument_key, underlying_key, expiry_date,
                       contract_ltp, captured_at, context_json
                FROM trade_context
                WHERE instrument_key = ? AND trigger = 'placement'
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """,
                (instrument_key,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["context"] = json.loads(result.pop("context_json"))
        return result

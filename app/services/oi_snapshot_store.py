from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings


class OISnapshotStore:
    """SQLite persistence for short-lived, five-minute OI analytics snapshots.

    A connection is opened per operation. This keeps the store safe when its small blocking
    operations are dispatched through ``asyncio.to_thread`` and also lets multiple API workers
    share the same database. WAL mode keeps readers from blocking the single writer.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.oi_database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oi_snapshots (
                    id INTEGER PRIMARY KEY,
                    underlying_key TEXT NOT NULL,
                    underlying_symbol TEXT NOT NULL,
                    expiry_date TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    slot_start TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    total_call_oi REAL,
                    total_put_oi REAL,
                    pcr REAL,
                    max_pain REAL,
                    payload_json TEXT NOT NULL,
                    UNIQUE (underlying_key, expiry_date, slot_start)
                );

                CREATE INDEX IF NOT EXISTS ix_oi_snapshots_expiry
                    ON oi_snapshots (expiry_date);
                CREATE INDEX IF NOT EXISTS ix_oi_snapshots_lookup
                    ON oi_snapshots (underlying_key, expiry_date, slot_start);

                CREATE TABLE IF NOT EXISTS oi_strikes (
                    snapshot_id INTEGER NOT NULL REFERENCES oi_snapshots(id) ON DELETE CASCADE,
                    strike_price REAL NOT NULL,
                    call_oi REAL,
                    put_oi REAL,
                    call_change_oi REAL,
                    put_change_oi REAL,
                    PRIMARY KEY (snapshot_id, strike_price)
                );
                """,
            )

    def has_snapshot(self, underlying_key: str, expiry_date: str, slot_start: datetime) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM oi_snapshots
                WHERE underlying_key = ? AND expiry_date = ? AND slot_start = ?
                LIMIT 1
                """,
                (underlying_key, expiry_date, _timestamp(slot_start)),
            ).fetchone()
        return row is not None

    def save_snapshot(
        self,
        *,
        underlying_key: str,
        underlying_symbol: str,
        expiry_date: str,
        slot_start: datetime,
        observed_at: datetime,
        analysis: dict[str, Any],
    ) -> bool:
        """Atomically store a snapshot and its strike rows.

        Returns ``False`` when another worker has already inserted the same five-minute slot.
        """
        oi = _object(analysis.get("oi"))
        pcr = _object(analysis.get("pcr"))
        max_pain = _object(analysis.get("max_pain"))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO oi_snapshots (
                    underlying_key, underlying_symbol, expiry_date, trading_date,
                    slot_start, observed_at, total_call_oi, total_put_oi, pcr,
                    max_pain, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    underlying_key,
                    underlying_symbol,
                    expiry_date,
                    slot_start.date().isoformat(),
                    _timestamp(slot_start),
                    _timestamp(observed_at),
                    _number(oi.get("total_calls")),
                    _number(oi.get("total_puts")),
                    _number(pcr.get("pcr")),
                    _number(max_pain.get("max_pain")),
                    json.dumps(analysis, separators=(",", ":"), sort_keys=True),
                ),
            )
            if cursor.rowcount == 0:
                return False
            snapshot_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO oi_strikes (
                    snapshot_id, strike_price, call_oi, put_oi,
                    call_change_oi, put_change_oi
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        strike,
                        values.get("call_oi"),
                        values.get("put_oi"),
                        values.get("call_change_oi"),
                        values.get("put_change_oi"),
                    )
                    for strike, values in _strike_values(analysis).items()
                ],
            )
        return True

    def delete_expired_before(self, cutoff: date) -> int:
        """Delete expiries from earlier calendar days, returning snapshot rows removed."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM oi_snapshots WHERE expiry_date < ?",
                (cutoff.isoformat(),),
            )
        return cursor.rowcount


def _strike_values(analysis: dict[str, Any]) -> dict[float, dict[str, float | None]]:
    combined: dict[float, dict[str, float | None]] = {}
    sections = (
        (_object(analysis.get("oi")), ("call_oi", "put_oi")),
        (_object(analysis.get("change_oi")), ("call_change_oi", "put_change_oi")),
    )
    for section, fields in sections:
        rows = section.get("call_put_oi_data_list")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or _number(row.get("strike_price")) is None:
                continue
            strike = _number(row["strike_price"])
            assert strike is not None
            values = combined.setdefault(strike, {})
            for field in fields:
                values[field] = _number(row.get(field))
    return combined


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("OI snapshot timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")

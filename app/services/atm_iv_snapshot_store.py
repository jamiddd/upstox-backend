from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import Settings


@dataclass(frozen=True)
class AtmIvPercentile:
    underlying_key: str
    current_atm_iv: float
    percentile: float
    sample_count: int
    min_atm_iv: float
    max_atm_iv: float
    window_start: str
    window_end: str


class AtmIvSnapshotStore:
    """SQLite persistence for five-minute ATM IV snapshots, one row per underlying per market
    slot -- same connection-per-operation/WAL posture as `OISnapshotStore`, but a single flat
    table since ATM IV is one number per slot, not a per-strike breakdown.

    Retention is a rolling window (see `delete_older_than`), not "through expiry day" like OI --
    ATM IV always uses whichever expiry is nearest at capture time (see
    `atm_iv_snapshot_collector.py`), which rolls week to week, so the stored history isn't
    scoped to one fixed contract the way OI's per-strike rows are. The user's own choice: keep
    a rolling ~1 month of samples ("only monthly"), not a full year, trading percentile precision
    for a much smaller/cheaper store.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.atm_iv_database_path)
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS atm_iv_snapshots (
                    id INTEGER PRIMARY KEY,
                    underlying_key TEXT NOT NULL,
                    underlying_symbol TEXT NOT NULL,
                    expiry_date TEXT NOT NULL,
                    slot_start TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    spot_price REAL,
                    atm_strike REAL,
                    call_iv REAL,
                    put_iv REAL,
                    atm_iv REAL NOT NULL,
                    UNIQUE (underlying_key, slot_start)
                );

                CREATE INDEX IF NOT EXISTS ix_atm_iv_snapshots_lookup
                    ON atm_iv_snapshots (underlying_key, slot_start);
                """,
            )

    def has_snapshot(self, underlying_key: str, slot_start: datetime) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM atm_iv_snapshots WHERE underlying_key = ? AND slot_start = ? LIMIT 1",
                (underlying_key, _timestamp(slot_start)),
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
        spot_price: float,
        atm_strike: float,
        call_iv: float,
        put_iv: float,
        atm_iv: float,
    ) -> bool:
        """Returns `False` when another worker already inserted the same five-minute slot."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO atm_iv_snapshots (
                    underlying_key, underlying_symbol, expiry_date, slot_start, observed_at,
                    spot_price, atm_strike, call_iv, put_iv, atm_iv
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    underlying_key,
                    underlying_symbol,
                    expiry_date,
                    _timestamp(slot_start),
                    _timestamp(observed_at),
                    spot_price,
                    atm_strike,
                    call_iv,
                    put_iv,
                    atm_iv,
                ),
            )
        return cursor.rowcount > 0

    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete every snapshot older than `cutoff`, returning rows removed -- the rolling-
        window retention (see this class's own doc comment), not a calendar/expiry-day cutoff."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM atm_iv_snapshots WHERE slot_start < ?",
                (_timestamp(cutoff),),
            )
        return cursor.rowcount

    def list_snapshots(
        self,
        *,
        underlying_key: str,
        limit: int = 3000,
    ) -> list[dict[str, Any]]:
        """Return stored slots newest-first -- the full rolling window is small enough (~75
        slots/trading day * ~21 trading days ~= 1600 rows) to hand the client the raw series for
        charting, rather than needing a server-side downsample."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT expiry_date, slot_start, observed_at, spot_price, atm_strike,
                       call_iv, put_iv, atm_iv
                FROM atm_iv_snapshots
                WHERE underlying_key = ?
                ORDER BY slot_start DESC
                LIMIT ?
                """,
                (underlying_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def percentile(self, *, underlying_key: str, current_atm_iv: float) -> Optional[AtmIvPercentile]:
        """Rank `current_atm_iv` against every stored sample for `underlying_key` -- the fraction
        of the rolling window's own history that reads at or below the current value, `[0, 100]`.
        `None` if nothing is stored yet for this underlying (a fresh deploy, or the collector
        hasn't captured its first slot today)."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS sample_count,
                    SUM(CASE WHEN atm_iv <= ? THEN 1 ELSE 0 END) AS at_or_below,
                    MIN(atm_iv) AS min_iv,
                    MAX(atm_iv) AS max_iv,
                    MIN(slot_start) AS window_start,
                    MAX(slot_start) AS window_end
                FROM atm_iv_snapshots
                WHERE underlying_key = ?
                """,
                (current_atm_iv, underlying_key),
            ).fetchone()
        sample_count = int(row["sample_count"] or 0)
        if sample_count == 0:
            return None
        at_or_below = int(row["at_or_below"] or 0)
        return AtmIvPercentile(
            underlying_key=underlying_key,
            current_atm_iv=current_atm_iv,
            percentile=(at_or_below / sample_count) * 100.0,
            sample_count=sample_count,
            min_atm_iv=float(row["min_iv"]),
            max_atm_iv=float(row["max_iv"]),
            window_start=str(row["window_start"]),
            window_end=str(row["window_end"]),
        )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("ATM IV snapshot timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")

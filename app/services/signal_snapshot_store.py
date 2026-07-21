from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import Settings


_METRIC_COLUMNS = (
    "atr",
    "vwap_distance",
    "level_distance",
    "pcr",
    "support_strike",
    "support_oi",
    "support_call_oi",
    "resistance_strike",
    "resistance_oi",
    "resistance_put_oi",
    "atm_straddle",
)


class SignalSnapshotStore:
    """Durable five-minute history for the metrics used by underlying-signal deltas."""

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.oi_database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS signal_snapshots (
                    id INTEGER PRIMARY KEY,
                    underlying_key TEXT NOT NULL,
                    expiry_date TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    slot_start TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    observed_epoch REAL NOT NULL,
                    atr REAL,
                    vwap_distance REAL,
                    level_distance REAL,
                    pcr REAL,
                    support_strike REAL,
                    support_oi REAL,
                    support_call_oi REAL,
                    resistance_strike REAL,
                    resistance_oi REAL,
                    resistance_put_oi REAL,
                    atm_straddle REAL,
                    UNIQUE (underlying_key, expiry_date, slot_start)
                );

                CREATE INDEX IF NOT EXISTS ix_signal_snapshots_lookup
                    ON signal_snapshots (underlying_key, expiry_date, observed_epoch);
                CREATE INDEX IF NOT EXISTS ix_signal_snapshots_expiry
                    ON signal_snapshots (expiry_date);
                """,
            )

    def record_and_find_previous(
        self,
        *,
        underlying_key: str,
        expiry_date: Optional[str],
        observed_at: datetime,
        metrics: dict[str, Optional[float]],
        minimum_age_seconds: float,
        maximum_age_seconds: float,
        target_age_seconds: float,
    ) -> Optional[dict[str, Any]]:
        """Insert the current slot and return the closest snapshot in the requested age band."""
        observed_at = _aware_utc(observed_at)
        observed_epoch = observed_at.timestamp()
        expiry_key = expiry_date or ""
        slot_start = observed_at.replace(
            minute=observed_at.minute - observed_at.minute % 5,
            second=0,
            microsecond=0,
        )
        values = tuple(_number(metrics.get(column)) for column in _METRIC_COLUMNS)

        with self._connect() as connection:
            previous = connection.execute(
                """
                SELECT observed_epoch, atr, vwap_distance, level_distance, pcr,
                       support_strike, support_oi, support_call_oi,
                       resistance_strike, resistance_oi, resistance_put_oi, atm_straddle
                FROM signal_snapshots
                WHERE underlying_key = ? AND expiry_date = ?
                  AND observed_epoch BETWEEN ? AND ?
                ORDER BY ABS(observed_epoch - ?) ASC
                LIMIT 1
                """,
                (
                    underlying_key,
                    expiry_key,
                    observed_epoch - maximum_age_seconds,
                    observed_epoch - minimum_age_seconds,
                    observed_epoch - target_age_seconds,
                ),
            ).fetchone()
            connection.execute(
                f"""
                INSERT OR IGNORE INTO signal_snapshots (
                    underlying_key, expiry_date, trading_date, slot_start,
                    observed_at, observed_epoch, {", ".join(_METRIC_COLUMNS)}
                ) VALUES ({", ".join("?" for _ in range(6 + len(_METRIC_COLUMNS)))})
                """,
                (
                    underlying_key,
                    expiry_key,
                    observed_at.date().isoformat(),
                    _timestamp(slot_start),
                    _timestamp(observed_at),
                    observed_epoch,
                    *values,
                ),
            )

        return dict(previous) if previous is not None else None

    def list_snapshots(
        self,
        *,
        underlying_key: str,
        expiry_date: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        filters = ["underlying_key = ?"]
        values: list[Any] = [underlying_key]
        if expiry_date is not None:
            filters.append("expiry_date = ?")
            values.append(expiry_date)
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT expiry_date, trading_date, slot_start, observed_at,
                       {", ".join(_METRIC_COLUMNS)}
                FROM signal_snapshots
                WHERE {" AND ".join(filters)}
                ORDER BY observed_epoch DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        snapshots = [dict(row) for row in rows]
        for snapshot in snapshots:
            snapshot["expiry_date"] = snapshot["expiry_date"] or None
        return snapshots

    def delete_expired_before(self, cutoff: date) -> int:
        """Delete dated signal snapshots after their complete expiry day has passed."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM signal_snapshots WHERE expiry_date != '' AND expiry_date < ?",
                (cutoff.isoformat(),),
            )
        return cursor.rowcount


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Signal snapshot timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="seconds")

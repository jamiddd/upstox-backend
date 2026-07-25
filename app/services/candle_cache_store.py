from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.core.config import Settings


class CandleCacheStore:
    """Persistent last-known candle rows used to bridge Upstox's overnight publication gap.

    The same SQLite file as OI history is reused with an independent table. Connections are short
    lived and WAL-backed, matching OISnapshotStore's multi-worker posture.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.oi_database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candle_cache (
                    instrument_key TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    interval INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (instrument_key, unit, interval, timestamp)
                )
                """,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def save(
        self,
        instrument_key: str,
        unit: str,
        interval: int,
        candles: list[dict[str, Any]],
    ) -> None:
        rows = [
            (instrument_key, unit, interval, candle["timestamp"], json.dumps(candle))
            for candle in candles
            if isinstance(candle.get("timestamp"), str)
        ]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO candle_cache (
                    instrument_key, unit, interval, timestamp, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (instrument_key, unit, interval, timestamp)
                DO UPDATE SET payload_json = excluded.payload_json
                """,
                rows,
            )

    def load(
        self,
        instrument_key: str,
        unit: str,
        interval: int,
        *,
        from_timestamp: str,
        to_timestamp: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM candle_cache
                WHERE instrument_key = ? AND unit = ? AND interval = ?
                  AND timestamp >= ? AND timestamp < ?
                ORDER BY timestamp
                """,
                (instrument_key, unit, interval, from_timestamp, to_timestamp),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

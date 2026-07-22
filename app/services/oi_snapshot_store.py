from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import Settings


@dataclass(frozen=True)
class OiStrikeDiff:
    strike_price: float
    call_oi_change: float
    put_oi_change: float
    # The strike's *absolute* OI as of the later (``to_slot``) snapshot -- not diffed, the actual
    # level. Lets a caller (the Android OI chart) render this exactly like its own default
    # since-yesterday's-close view (bar height = current level, capped change on top of it) with
    # the FROM/TO range standing in for "yesterday's close -> now", rather than needing a second,
    # delta-only chart shape just for this view. 0.0 if the strike wasn't listed in ``to_slot``'s
    # own snapshot (matches call_oi_change/put_oi_change's own zero-baseline convention).
    call_oi: float
    put_oi: float


@dataclass(frozen=True)
class OiStrikesDiff:
    underlying_symbol: str
    total_call_oi_change: float
    total_put_oi_change: float
    strikes: list[OiStrikeDiff]


class SnapshotNotFoundError(Exception):
    """Raised when one side of an exact-slot OI comparison is unavailable."""

    def __init__(self, *, slot: datetime, which: str) -> None:
        self.slot = slot
        self.which = which
        super().__init__(f"{which} snapshot was not found for slot {_timestamp(slot)}")


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
        connection.row_factory = sqlite3.Row
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

    def list_snapshots(
        self,
        *,
        underlying_key: str,
        expiry_date: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return stored slot metadata (no per-strike rows), newest-first.

        A plain query over ``oi_snapshots`` keeps time-point pickers from loading ``oi_strikes``
        or ``payload_json`` for every candidate slot.
        """
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
                       total_call_oi, total_put_oi, pcr, max_pain
                FROM oi_snapshots
                WHERE {" AND ".join(filters)}
                ORDER BY slot_start DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def diff_strikes(
        self,
        *,
        underlying_key: str,
        expiry_date: str,
        from_slot: datetime,
        to_slot: datetime,
    ) -> OiStrikesDiff:
        """Compute per-strike call/put OI changes between two exact stored slots, plus each
        strike's absolute OI as of ``to_slot`` (see ``OiStrikeDiff.call_oi``/``put_oi``).

        A strike or call/put value missing from either snapshot is treated as zero, matching the
        mobile option-chain/GEX convention. Raises ``SnapshotNotFoundError`` for the first missing
        snapshot, naming whether it was ``from_slot`` or ``to_slot``.
        """
        from_key = _timestamp(from_slot)
        to_key = _timestamp(to_slot)
        with self._connect() as connection:
            # Pin both metadata and strike reads to one consistent SQLite snapshot. Rows are
            # immutable after insertion, but overnight expiry cleanup may run concurrently.
            connection.execute("BEGIN")
            snapshot_rows = connection.execute(
                """
                SELECT id, underlying_symbol, slot_start, total_call_oi, total_put_oi
                FROM oi_snapshots
                WHERE underlying_key = ? AND expiry_date = ?
                  AND slot_start IN (?, ?)
                """,
                (underlying_key, expiry_date, from_key, to_key),
            ).fetchall()
            by_slot = {row["slot_start"]: row for row in snapshot_rows}
            if from_key not in by_slot:
                raise SnapshotNotFoundError(slot=from_slot, which="from_slot")
            if to_key not in by_slot:
                raise SnapshotNotFoundError(slot=to_slot, which="to_slot")
            before = by_slot[from_key]
            after = by_slot[to_key]
            strike_rows = connection.execute(
                """
                SELECT snapshot_id, strike_price, call_oi, put_oi
                FROM oi_strikes
                WHERE snapshot_id IN (?, ?)
                """,
                (before["id"], after["id"]),
            ).fetchall()

        before_strikes = {
            float(row["strike_price"]): row for row in strike_rows if row["snapshot_id"] == before["id"]
        }
        after_strikes = {
            float(row["strike_price"]): row for row in strike_rows if row["snapshot_id"] == after["id"]
        }
        strikes: list[OiStrikeDiff] = []
        for strike_price in sorted(before_strikes.keys() | after_strikes.keys()):
            before_row = before_strikes.get(strike_price)
            after_row = after_strikes.get(strike_price)
            after_call_oi = _number_or_zero(after_row["call_oi"] if after_row is not None else None)
            after_put_oi = _number_or_zero(after_row["put_oi"] if after_row is not None else None)
            strikes.append(
                OiStrikeDiff(
                    strike_price=strike_price,
                    call_oi_change=(
                        after_call_oi
                        - _number_or_zero(before_row["call_oi"] if before_row is not None else None)
                    ),
                    put_oi_change=(
                        after_put_oi
                        - _number_or_zero(before_row["put_oi"] if before_row is not None else None)
                    ),
                    call_oi=after_call_oi,
                    put_oi=after_put_oi,
                ),
            )

        return OiStrikesDiff(
            underlying_symbol=str(after["underlying_symbol"]),
            total_call_oi_change=(
                _number_or_zero(after["total_call_oi"]) - _number_or_zero(before["total_call_oi"])
            ),
            total_put_oi_change=(
                _number_or_zero(after["total_put_oi"]) - _number_or_zero(before["total_put_oi"])
            ),
            strikes=strikes,
        )

    def find_snapshot_strikes_in_band(
        self,
        *,
        underlying_key: str,
        expiry_date: str,
        now: datetime,
        minimum_age_seconds: float,
        maximum_age_seconds: float,
        target_age_seconds: float,
    ) -> Optional[dict[float, tuple[Optional[float], Optional[float]]]]:
        """Return ``{strike_price: (call_oi, put_oi)}`` for whichever persisted snapshot (see
        ``save_snapshot``) is closest to ``target_age_seconds`` old, within
        ``[minimum_age_seconds, maximum_age_seconds]`` of ``now`` -- ``None`` if nothing was
        recorded in that window.

        This is the same per-strike history ``GET /api/main/oi-snapshots/history``/``/diff``
        read (see ``list_snapshots``/``diff_strikes`` above) -- reused by
        ``UnderlyingSignalsService``'s OI(S)/OI(R) 5-minute-change tags so that lookup can ask
        "what was *this* strike's OI ~5 minutes ago" for whichever strike is support/resistance
        *right now*, rather than requiring the same strike to have already been support/
        resistance back then. One canonical per-strike-OI-over-time store, not two.
        """
        now_key = _timestamp(now)
        with self._connect() as connection:
            snapshot = connection.execute(
                """
                SELECT id
                FROM oi_snapshots
                WHERE underlying_key = ? AND expiry_date = ?
                  AND (julianday(?) - julianday(observed_at)) * 86400.0 BETWEEN ? AND ?
                ORDER BY ABS((julianday(?) - julianday(observed_at)) * 86400.0 - ?) ASC
                LIMIT 1
                """,
                (
                    underlying_key, expiry_date,
                    now_key, minimum_age_seconds, maximum_age_seconds,
                    now_key, target_age_seconds,
                ),
            ).fetchone()
            if snapshot is None:
                return None
            strike_rows = connection.execute(
                "SELECT strike_price, call_oi, put_oi FROM oi_strikes WHERE snapshot_id = ?",
                (snapshot["id"],),
            ).fetchall()
        return {float(row["strike_price"]): (row["call_oi"], row["put_oi"]) for row in strike_rows}


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


def _number_or_zero(value: Any) -> float:
    return _number(value) or 0.0


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("OI snapshot timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")

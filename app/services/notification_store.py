from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import Settings

# Severities ordered low-to-high so "at least warning" style filters (used by both the
# unread-count push-threshold check and a future min-severity query filter) can be expressed as a
# simple slice rather than hardcoding pairwise comparisons.
SEVERITIES = ("info", "warning", "critical")


class NotificationStore:
    """SQLite persistence for the backend-generated notification log (see `record_notification`
    in `notification_service.py` -- every notification-worthy event across this backend goes
    through that one function, which calls into this store).

    Same posture as `OISnapshotStore`: a connection is opened per operation (safe across multiple
    API workers and `asyncio.to_thread` dispatch), WAL mode keeps readers from blocking the single
    writer, and the schema is created eagerly and idempotently on every process start.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.notification_database_path)
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
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT,
                    created_at TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    read_at TEXT
                );

                CREATE INDEX IF NOT EXISTS ix_notifications_created
                    ON notifications (created_epoch);
                CREATE INDEX IF NOT EXISTS ix_notifications_category
                    ON notifications (category);
                CREATE INDEX IF NOT EXISTS ix_notifications_severity
                    ON notifications (severity);
                CREATE INDEX IF NOT EXISTS ix_notifications_unread
                    ON notifications (read_at);
                """,
            )

    def record(
        self,
        *,
        category: str,
        severity: str,
        title: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Persists one notification and returns it in the same shape `list_notifications` rows
        use, so a caller (e.g. the stream dispatch) can forward the freshly created row to
        connected clients without a second read."""
        if severity not in SEVERITIES:
            raise ValueError(f"Unknown severity {severity!r}")
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        created_at = moment.isoformat(timespec="seconds")
        created_epoch = moment.timestamp()
        details_json = json.dumps(details, separators=(",", ":"), sort_keys=True) if details else None
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO notifications (
                    category, severity, title, message, details_json, created_at, created_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (category, severity, title, message, details_json, created_at, created_epoch),
            )
            notification_id = int(cursor.lastrowid)
        return {
            "id": notification_id,
            "category": category,
            "severity": severity,
            "title": title,
            "message": message,
            "details": details,
            "created_at": created_at,
            "read_at": None,
        }

    def list_notifications(
        self,
        *,
        category: Optional[str] = None,
        severity: Optional[str] = None,
        unread_only: bool = False,
        page_number: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Returns `(items, page)` newest-first -- `page` matches the same shape
        `order_history_service._paginate` already uses. Real `LIMIT`/`OFFSET` + `COUNT(*)`, not an
        in-memory slice, since this table isn't bounded the way a single day's order book is."""
        filters: list[str] = []
        values: list[Any] = []
        if category is not None:
            filters.append("category = ?")
            values.append(category)
        if severity is not None:
            filters.append("severity = ?")
            values.append(severity)
        if unread_only:
            filters.append("read_at IS NULL")
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        with self._connect() as connection:
            total_records = connection.execute(
                f"SELECT COUNT(*) FROM notifications {where_clause}", values,
            ).fetchone()[0]
            offset = (page_number - 1) * page_size
            rows = connection.execute(
                f"""
                SELECT id, category, severity, title, message, details_json, created_at, read_at
                FROM notifications
                {where_clause}
                ORDER BY created_epoch DESC
                LIMIT ? OFFSET ?
                """,
                [*values, page_size, offset],
            ).fetchall()

        items = [_row_to_dict(row) for row in rows]
        total_pages = (total_records + page_size - 1) // page_size if total_records else 0
        page = {
            "page_number": page_number,
            "page_size": page_size,
            "total_records": total_records,
            "total_pages": total_pages,
        }
        return items, page

    def mark_read(self, notification_id: int, *, now: Optional[datetime] = None) -> bool:
        """Marks one notification read. Returns False if it didn't exist or was already read (an
        idempotent no-op, not an error, matching this backend's general "missing means no-op"
        posture)."""
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
                (moment.isoformat(timespec="seconds"), notification_id),
            )
        return cursor.rowcount > 0

    def mark_all_read(self, *, now: Optional[datetime] = None) -> int:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE notifications SET read_at = ? WHERE read_at IS NULL",
                (moment.isoformat(timespec="seconds"),),
            )
        return cursor.rowcount

    def unread_count(self, *, min_severity: Optional[str] = None) -> int:
        """Count of unread notifications, optionally restricted to `min_severity` and anything
        more severe (e.g. `min_severity="warning"` counts warning + critical, not info)."""
        filters = ["read_at IS NULL"]
        values: list[Any] = []
        if min_severity is not None:
            if min_severity not in SEVERITIES:
                raise ValueError(f"Unknown severity {min_severity!r}")
            at_least = SEVERITIES[SEVERITIES.index(min_severity):]
            filters.append(f"severity IN ({','.join('?' * len(at_least))})")
            values.extend(at_least)
        with self._connect() as connection:
            count = connection.execute(
                f"SELECT COUNT(*) FROM notifications WHERE {' AND '.join(filters)}", values,
            ).fetchone()[0]
        return int(count)

    def delete_expired_before(self, cutoff: date) -> int:
        """Deletes notifications created before `cutoff` (calendar day, UTC), returning the count
        removed -- same retention shape as the other stores' own `delete_expired_before`."""
        cutoff_epoch = datetime.combine(cutoff, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM notifications WHERE created_epoch < ?", (cutoff_epoch,),
            )
        return cursor.rowcount


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    details_json = row["details_json"]
    return {
        "id": row["id"],
        "category": row["category"],
        "severity": row["severity"],
        "title": row["title"],
        "message": row["message"],
        "details": json.loads(details_json) if details_json else None,
        "created_at": row["created_at"],
        "read_at": row["read_at"],
    }

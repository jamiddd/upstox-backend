from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.notification_store import NotificationStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="k",
        upstox_api_secret="s",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=tmp_path / "token.enc",
        notification_database_path=tmp_path / "notifications.sqlite3",
    )


def _store(tmp_path: Path) -> NotificationStore:
    return NotificationStore(_settings(tmp_path))


def test_record_returns_the_created_row(tmp_path: Path) -> None:
    store = _store(tmp_path)

    row = store.record(category="auth", severity="critical", title="Login expired", message="Please re-login.")

    assert row["id"] > 0
    assert row["category"] == "auth"
    assert row["severity"] == "critical"
    assert row["title"] == "Login expired"
    assert row["message"] == "Please re-login."
    assert row["details"] is None
    assert row["read_at"] is None
    assert row["created_at"]


def test_record_persists_details_json(tmp_path: Path) -> None:
    store = _store(tmp_path)

    row = store.record(
        category="risk", severity="critical", title="Max loss", message="Exited.",
        details={"positions_found": 2, "results": [{"status": "success"}]},
    )
    fetched, _ = store.list_notifications()

    assert fetched[0]["details"] == {"positions_found": 2, "results": [{"status": "success"}]}
    assert row["details"] == fetched[0]["details"]


def test_record_rejects_unknown_severity(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError):
        store.record(category="auth", severity="urgent", title="x", message="y")


def test_list_notifications_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(category="auth", severity="info", title="first", message="m", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    store.record(category="auth", severity="info", title="second", message="m", now=datetime(2026, 1, 2, tzinfo=timezone.utc))

    items, page = store.list_notifications()

    assert [item["title"] for item in items] == ["second", "first"]
    assert page == {"page_number": 1, "page_size": 20, "total_records": 2, "total_pages": 1}


def test_list_notifications_filters_by_category_and_severity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(category="auth", severity="critical", title="a", message="m")
    store.record(category="feed", severity="warning", title="b", message="m")
    store.record(category="auth", severity="warning", title="c", message="m")

    by_category, _ = store.list_notifications(category="auth")
    by_severity, _ = store.list_notifications(severity="warning")

    assert {item["title"] for item in by_category} == {"a", "c"}
    assert {item["title"] for item in by_severity} == {"b", "c"}


def test_list_notifications_unread_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    unread = store.record(category="auth", severity="info", title="unread", message="m")
    read = store.record(category="auth", severity="info", title="read", message="m")
    store.mark_read(read["id"])

    items, page = store.list_notifications(unread_only=True)

    assert [item["id"] for item in items] == [unread["id"]]
    assert page["total_records"] == 1


def test_list_notifications_pagination(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(5):
        store.record(category="auth", severity="info", title=f"n{i}", message="m")

    page1, meta1 = store.list_notifications(page_number=1, page_size=2)
    page2, meta2 = store.list_notifications(page_number=2, page_size=2)

    assert len(page1) == 2
    assert len(page2) == 2
    assert meta1["total_pages"] == 3
    assert meta2["page_number"] == 2
    assert {item["title"] for item in page1} != {item["title"] for item in page2}


def test_mark_read_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = store.record(category="auth", severity="info", title="a", message="m")

    first = store.mark_read(row["id"])
    second = store.mark_read(row["id"])

    assert first is True
    assert second is False


def test_mark_read_missing_id_returns_false(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.mark_read(999) is False


def test_mark_all_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(category="auth", severity="info", title="a", message="m")
    store.record(category="auth", severity="info", title="b", message="m")

    updated = store.mark_all_read()

    assert updated == 2
    assert store.unread_count() == 0


def test_unread_count_with_min_severity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(category="auth", severity="info", title="a", message="m")
    store.record(category="auth", severity="warning", title="b", message="m")
    store.record(category="auth", severity="critical", title="c", message="m")

    assert store.unread_count() == 3
    assert store.unread_count(min_severity="warning") == 2
    assert store.unread_count(min_severity="critical") == 1


def test_delete_expired_before(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(
        category="auth", severity="info", title="old", message="m",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    store.record(
        category="auth", severity="info", title="new", message="m",
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    deleted = store.delete_expired_before(date(2026, 3, 1))

    items, _ = store.list_notifications()
    assert deleted == 1
    assert [item["title"] for item in items] == ["new"]

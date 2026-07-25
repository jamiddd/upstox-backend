from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_device_token_store, get_notification_store
from app.api.routes import router
from app.core.config import Settings, get_settings
from app.services.device_token_store import DeviceTokenStore
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
        device_token_path=tmp_path / "device_token.json",
    )


def _client(tmp_path: Path) -> tuple[TestClient, NotificationStore]:
    settings = _settings(tmp_path)
    store = NotificationStore(settings)
    device_token_store = DeviceTokenStore(settings)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_notification_store] = lambda: store
    app.dependency_overrides[get_device_token_store] = lambda: device_token_store
    return TestClient(app), store


_HEADERS = {"X-API-Key": "mobile-secret"}


def test_list_notifications_requires_api_key(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.get("/api/notifications")

    assert response.status_code == 401


def test_list_notifications_empty_store(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.get("/api/notifications", headers=_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "notifications": [],
        "page": {"page_number": 1, "page_size": 20, "total_records": 0, "total_pages": 0},
        "unread_count": 0,
    }


def test_list_notifications_returns_recorded_rows_and_unread_count(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    store.record(category="auth", severity="critical", title="Login expired", message="m")
    store.record(category="feed", severity="warning", title="Feed reconnecting", message="m")

    response = client.get("/api/notifications", headers=_HEADERS)

    body = response.json()
    assert body["page"]["total_records"] == 2
    assert body["unread_count"] == 2
    assert {item["title"] for item in body["notifications"]} == {"Login expired", "Feed reconnecting"}


def test_list_notifications_filters_by_severity_and_category(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    store.record(category="auth", severity="critical", title="a", message="m")
    store.record(category="feed", severity="warning", title="b", message="m")

    response = client.get(
        "/api/notifications", headers=_HEADERS, params={"severity": "critical", "category": "auth"},
    )

    body = response.json()
    assert [item["title"] for item in body["notifications"]] == ["a"]


def test_mark_notification_read(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    row = store.record(category="auth", severity="info", title="a", message="m")

    response = client.post(f"/api/notifications/{row['id']}/read", headers=_HEADERS)

    body = response.json()
    assert body["updated"] is True
    assert body["unread_count"] == 0


def test_mark_notification_read_is_idempotent(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    row = store.record(category="auth", severity="info", title="a", message="m")
    client.post(f"/api/notifications/{row['id']}/read", headers=_HEADERS)

    response = client.post(f"/api/notifications/{row['id']}/read", headers=_HEADERS)

    assert response.json()["updated"] is False


def test_mark_all_notifications_read(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    store.record(category="auth", severity="info", title="a", message="m")
    store.record(category="auth", severity="info", title="b", message="m")

    response = client.post("/api/notifications/read-all", headers=_HEADERS)

    body = response.json()
    assert body["updated"] == 2
    assert body["unread_count"] == 0


def test_register_device_saves_token_and_preference(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    settings = _settings(tmp_path)

    response = client.post(
        "/api/notifications/register-device",
        headers=_HEADERS,
        json={"fcm_token": "device-token-123", "push_preference": "everything"},
    )

    assert response.status_code == 200
    token, preference = DeviceTokenStore(settings).load()
    assert token == "device-token-123"
    assert preference == "everything"


def test_register_device_rejects_unknown_preference(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.post(
        "/api/notifications/register-device",
        headers=_HEADERS,
        json={"fcm_token": "abc", "push_preference": "sometimes"},
    )

    assert response.status_code == 422

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app


def _settings() -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/token.enc"),
    )


def test_protected_route_rejects_missing_api_key() -> None:
    """Ensure /api routes require the mobile API key."""
    app.dependency_overrides[get_settings] = _settings
    try:
        response = TestClient(app).get("/api/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.json() == {
        "status": "error",
        "message": "Invalid or missing API key",
    }


def test_protected_route_accepts_valid_api_key() -> None:
    """Ensure the configured mobile API key unlocks /api routes."""
    app.dependency_overrides[get_settings] = _settings
    try:
        response = TestClient(app).get(
            "/api/status",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_auth_status_reports_invalid_token_store_config() -> None:
    """Return a useful error when token encryption config is invalid."""
    app.dependency_overrides[get_settings] = _settings
    try:
        response = TestClient(app).get(
            "/api/auth/status",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json() == {
        "status": "error",
        "message": "TOKEN_ENCRYPTION_KEY is not configured",
    }

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.config import Settings, get_settings
from app.core.security import require_web_session
from app.core.web_session import (
    WEB_SESSION_COOKIE_NAME,
    create_session_token,
    verify_session_token,
)
from app.main import app


def _request_with_cookie(cookie_value: str | None) -> Request:
    headers = []
    if cookie_value is not None:
        headers.append((b"cookie", f"{WEB_SESSION_COOKIE_NAME}={cookie_value}".encode()))
    scope = {"type": "http", "headers": headers, "method": "GET", "path": "/"}
    return Request(scope)


def _settings(web_session_secret: str = "web-secret") -> Settings:
    # Deliberately no **kwargs here -- this doubles as a FastAPI dependency-override callable
    # (see get_settings overrides below), and FastAPI introspects an override's own signature the
    # same way it would a real dependency, so a catch-all **kwargs parameter breaks request
    # parameter binding (manifests as a spurious 422, not an obvious error).
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/token.enc"),
        web_session_secret=web_session_secret,
    )


def test_token_round_trip_is_valid() -> None:
    settings = _settings()
    token = create_session_token(settings, issued_at=1_000.0)
    assert verify_session_token(token, settings, now=1_000.0)


def test_token_expires_after_ttl() -> None:
    settings = _settings()
    token = create_session_token(settings, issued_at=1_000.0)
    assert not verify_session_token(token, settings, ttl_seconds=60, now=1_061.0)


def test_token_rejects_tampering() -> None:
    settings = _settings()
    token = create_session_token(settings, issued_at=1_000.0)
    payload, signature = token.split(".", 1)
    tampered = f"{payload}.{'0' * len(signature)}"
    assert not verify_session_token(tampered, settings, now=1_000.0)


def test_token_rejects_wrong_secret() -> None:
    signed_with = _settings("secret-a")
    verified_with = _settings("secret-b")
    token = create_session_token(signed_with, issued_at=1_000.0)
    assert not verify_session_token(token, verified_with, now=1_000.0)


def test_web_login_sets_session_cookie() -> None:
    app.dependency_overrides[get_settings] = _settings
    try:
        response = TestClient(app).post(
            "/api/auth/web-login",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert WEB_SESSION_COOKIE_NAME in response.cookies


def test_web_login_rejects_missing_api_key() -> None:
    app.dependency_overrides[get_settings] = _settings
    try:
        response = TestClient(app).post("/api/auth/web-login")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_require_web_session_accepts_valid_cookie() -> None:
    settings = _settings()
    token = create_session_token(settings)
    require_web_session(_request_with_cookie(token), app_settings=settings)


def test_require_web_session_rejects_missing_cookie() -> None:
    settings = _settings()
    try:
        require_web_session(_request_with_cookie(None), app_settings=settings)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 401


def test_require_web_session_rejects_invalid_cookie() -> None:
    settings = _settings()
    try:
        require_web_session(_request_with_cookie("garbage"), app_settings=settings)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 401


def test_web_session_status_round_trip() -> None:
    app.dependency_overrides[get_settings] = _settings
    try:
        # base_url must be https:// -- the cookie web-login sets is Secure, and httpx's cookie jar
        # (correctly) refuses to send a Secure cookie back over plain http, which the default
        # TestClient base_url ("http://testserver") is.
        client = TestClient(app, base_url="https://testserver")
        login = client.post("/api/auth/web-login", headers={"X-API-Key": "mobile-secret"})
        assert login.status_code == 200

        status_response = client.get("/api/auth/web-session-status")
        assert status_response.status_code == 200
        assert status_response.json() == {"authenticated": True}

        # No X-API-Key here -- the browser never retains it past web-login, so logout must be
        # reachable with only the session cookie the client already holds.
        logout_response = client.post("/api/auth/web-logout")
        assert logout_response.status_code == 200

        after_logout = client.get("/api/auth/web-session-status")
        assert after_logout.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_web_session_status_rejects_missing_cookie() -> None:
    app.dependency_overrides[get_settings] = _settings
    try:
        response = TestClient(app).get("/api/auth/web-session-status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_web_logout_rejects_missing_cookie() -> None:
    app.dependency_overrides[get_settings] = _settings
    try:
        response = TestClient(app).post("/api/auth/web-logout")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_web_logout_rejects_api_key_alone() -> None:
    """API key alone (no session cookie) must not unlock logout -- it's on web_router
    (require_web_session), not protected_router, precisely because a browser holding only the API
    key isn't a state that should occur post-login, but this pins the dependency choice regardless."""
    app.dependency_overrides[get_settings] = _settings
    try:
        response = TestClient(app).post(
            "/api/auth/web-logout",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401

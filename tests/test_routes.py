from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi.testclient import TestClient

from app.api.dependencies import get_token_store, get_upstox_service
from app.core.config import Settings, get_settings
from app.main import app


class FakeTokenStore:
    def __init__(self, *, token: Optional[str] = "upstox-token") -> None:
        self.token = token
        self.saved: Optional[dict[str, Any]] = None
        self.cleared = False

    def has_token(self) -> bool:
        return self.token is not None

    def save(self, token_payload: dict[str, Any]) -> None:
        self.saved = token_payload
        self.token = token_payload["access_token"]

    def load_access_token(self) -> str:
        if self.token is None:
            from app.core.exceptions import UpstoxAuthRequiredError

            raise UpstoxAuthRequiredError("Upstox login is required")
        return self.token

    def clear(self) -> None:
        self.cleared = True
        self.token = None


class FakeUpstoxService:
    def build_login_url(self, *, state: Optional[str] = None) -> str:
        return f"https://upstox.test/login?state={state}"

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        return {"access_token": f"token-for-{code}"}

    async def get_ltp(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        return {"status": "success", "data": {"token": access_token, "key": instrument_key}}

    async def get_quotes(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        return {"status": "success", "data": {"token": access_token, "key": instrument_key}}

    async def get_holdings(self, access_token: str) -> dict[str, Any]:
        return {"status": "success", "data": [{"token": access_token}]}

    async def get_positions(self, access_token: str) -> dict[str, Any]:
        return {"status": "success", "data": [{"token": access_token}]}


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


def _client(token_store: Optional[FakeTokenStore] = None) -> TestClient:
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_upstox_service] = FakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: token_store or FakeTokenStore()
    return TestClient(app)


def test_health_is_public() -> None:
    """The deployment health endpoint does not require app auth."""
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_auth_status_reports_stored_token() -> None:
    """Return whether an Upstox token is present."""
    client = _client(FakeTokenStore(token="upstox-token"))
    try:
        response = client.get("/api/auth/status", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"authenticated": True}


def test_auth_callback_saves_token() -> None:
    """Exchange the auth code and save the token payload."""
    token_store = FakeTokenStore(token=None)
    client = _client(token_store)
    try:
        response = client.get(
            "/api/auth/callback?code=abc",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "authenticated"}
    assert token_store.saved == {"access_token": "token-for-abc"}


def test_market_route_uses_stored_token() -> None:
    """Proxy market data calls through the Upstox service."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/market/ltp?instrument_key=NSE_EQ%7CINE848E01016",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["data"] == {
        "token": "stored-token",
        "key": "NSE_EQ|INE848E01016",
    }


def test_upstox_backed_route_requires_token() -> None:
    """Upstox-backed routes return 401 until OAuth has completed."""
    client = _client(FakeTokenStore(token=None))
    try:
        response = client.get(
            "/api/portfolio/holdings",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.json() == {"status": "error", "message": "Upstox login is required"}

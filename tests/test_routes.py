from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi.testclient import TestClient

from app.api.dependencies import get_token_store, get_upstox_service
from app.core.config import Settings, get_settings
from app.main import app
from app.services.main_screen_service import _CACHE


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
        quotes = {
            "NSE_INDEX|Nifty 50": {
                "instrument_token": "NSE_INDEX|Nifty 50",
                "last_price": 25050.0,
            },
            "NSE_FO|111": {
                "instrument_token": "NSE_FO|111",
                "last_price": 125.0,
                "depth": {
                    "buy": [{"price": 124.5}],
                    "sell": [{"price": 125.5}],
                },
            },
            "NSE_FO|222": {
                "instrument_token": "NSE_FO|222",
                "last_price": 90.0,
                "depth": {
                    "buy": [{"price": 89.5}],
                    "sell": [{"price": 90.5}],
                },
            },
        }
        return {
            "status": "success",
            "data": {
                key: quotes[key]
                for key in instrument_key.split(",")
                if key in quotes
            },
        }

    async def get_holdings(self, access_token: str) -> dict[str, Any]:
        return {"status": "success", "data": [{"token": access_token}]}

    async def get_positions(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "instrument_token": "NSE_FO|111",
                    "trading_symbol": "NIFTY26JUL25000CE",
                    "quantity": 75,
                    "average_price": 120.0,
                    "last_price": 125.0,
                    "pnl": 375.0,
                },
                {
                    "instrument_token": "NSE_FO|closed",
                    "trading_symbol": "NIFTY26JUL24000PE",
                    "quantity": 0,
                    "average_price": 80.0,
                    "last_price": 80.0,
                    "pnl": 25.0,
                },
            ],
        }

    async def get_option_contracts(
        self,
        access_token: str,
        instrument_key: str,
        *,
        expiry_date: Optional[str] = None,
    ) -> dict[str, Any]:
        contracts = [
            {
                "name": "NIFTY",
                "expiry": "2026-07-16",
                "instrument_key": "NSE_FO|111",
                "trading_symbol": "NIFTY26JUL25000CE",
                "instrument_type": "CE",
                "underlying_symbol": "NIFTY",
                "strike_price": 25000,
            },
            {
                "name": "NIFTY",
                "expiry": "2026-07-23",
                "instrument_key": "NSE_FO|222",
                "trading_symbol": "NIFTY26JUL25000PE",
                "instrument_type": "PE",
                "underlying_symbol": "NIFTY",
                "strike_price": 25000,
            },
        ]
        if expiry_date:
            contracts = [contract for contract in contracts if contract["expiry"] == expiry_date]
        return {"status": "success", "data": contracts}

    async def get_funds_and_margin(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "available_to_trade": {
                    "cash_available_to_trade": {
                        "cash": {
                            "opening_balance": 100000.0,
                        }
                    }
                }
            },
        }

    async def get_market_feed_authorize(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "authorized_redirect_uri": "wss://feed.test/socket?code=one-time",
            },
        }


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
    _CACHE.clear()
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
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "authenticated"}
    assert token_store.saved == {"access_token": "token-for-abc"}


def test_auth_callback_does_not_require_mobile_api_key() -> None:
    """Allow Upstox browser redirects to call the OAuth callback."""
    token_store = FakeTokenStore(token=None)
    client = _client(token_store)
    try:
        response = client.get("/api/auth/callback?code=redirect-code")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert token_store.saved == {"access_token": "token-for-redirect-code"}


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


def test_main_bootstrap_returns_screen_ready_payload() -> None:
    """Return initial main-screen data in the backend contract shape."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/bootstrap",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["underlying"] == {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "symbol": "NIFTY",
        "name": "NIFTY",
        "spot_price": 25050.0,
    }
    assert payload["expiries"] == ["2026-07-16", "2026-07-23"]
    assert payload["selected_expiry"] == "2026-07-16"
    assert payload["summary"] == {
        "opening_balance": 100000.0,
        "profit_loss": 375.0,
        "closing_balance": 100375.0,
    }
    assert payload["open_positions"] == [
        {
            "instrument_key": "NSE_FO|111",
            "trading_symbol": "NIFTY26JUL25000CE",
            "quantity": 75.0,
            "entry_price": 120.0,
            "last_price": 125.0,
            "pnl": 375.0,
        }
    ]


def test_main_selected_quote_returns_bid_and_ask_for_selected_strike() -> None:
    """Resolve the app-selected strike into a contract and return button prices."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/selected-quote"
            "?expiry_date=2026-07-16&strike_price=25000&option_type=CE",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "underlying": {
            "instrument_key": "NSE_INDEX|Nifty 50",
            "spot_price": 25050.0,
        },
        "contract": {
            "instrument_key": "NSE_FO|111",
            "trading_symbol": "NIFTY26JUL25000CE",
            "strike_price": 25000.0,
            "option_type": "CE",
            "ltp": 125.0,
            "bid_price": 124.5,
            "ask_price": 125.5,
        },
    }


def test_main_position_quotes_returns_ltp_for_requested_keys() -> None:
    """Return compact LTP data for open positions tracked in the app."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/position-quotes?instrument_keys=NSE_FO%7C111,NSE_FO%7C222",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "positions": [
            {"instrument_key": "NSE_FO|111", "ltp": 125.0},
            {"instrument_key": "NSE_FO|222", "ltp": 90.0},
        ]
    }


def test_main_summary_returns_balance_pnl_and_closing_balance() -> None:
    """Return the summary section payload."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/summary",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "opening_balance": 100000.0,
        "profit_loss": 375.0,
        "closing_balance": 100375.0,
    }


def test_market_feed_authorize_returns_one_time_websocket_url() -> None:
    """Return Upstox's one-time market feed WebSocket URL."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/market/feed/authorize",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "data": {
            "authorized_redirect_uri": "wss://feed.test/socket?code=one-time",
        },
    }

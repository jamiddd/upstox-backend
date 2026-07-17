from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi.testclient import TestClient

from app.api.dependencies import get_token_store, get_upstox_service
from app.core.config import Settings, get_settings
from app.main import app
from app.services import instrument_rules_service
from app.services.instrument_rules_service import _MasterCache
from app.services.main_screen_service import _CACHE
from app.services.search_screen_service import _SEARCH_CACHE


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

    async def get_profile(self, access_token: str) -> dict[str, Any]:
        if access_token == "expired-token":
            from app.core.exceptions import UpstoxApiError

            raise UpstoxApiError(
                "Invalid token used to access api",
                status_code=401,
                upstox_code="UDAPI100050",
            )
        return {"status": "success", "data": {"user_name": "Test User"}}

    async def get_ltp(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        return {"status": "success", "data": {"token": access_token, "key": instrument_key}}

    async def get_quotes(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        quotes = {
            "NSE_INDEX|Nifty 50": {
                "instrument_token": "NSE_INDEX|Nifty 50",
                "last_price": 25050.0,
                "ohlc": {
                    "open": 24900.0,
                    "high": 25100.0,
                    "low": 24850.0,
                    "close": 24950.0,
                },
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
            "GLOBAL_INDEX|^GSPC": {
                "instrument_token": "GLOBAL_INDEX|^GSPC",
                "last_price": 5555.5,
                "ohlc": {"open": 5500.0, "high": 5560.0, "low": 5495.0, "close": 5540.0},
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

    async def get_order_book(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "order_id": "order-older",
                    "instrument_token": "NSE_FO|111",
                    "trading_symbol": "NIFTY26JUL25000CE",
                    "transaction_type": "BUY",
                    "order_type": "LIMIT",
                    "product": "I",
                    "status": "complete",
                    "quantity": 75,
                    "filled_quantity": 75,
                    "pending_quantity": 0,
                    "price": 120.0,
                    "average_price": 119.5,
                    "trigger_price": 0,
                    "order_timestamp": "2026-07-13 09:20:00",
                    "exchange_timestamp": "2026-07-13 09:20:01",
                    "status_message": "",
                },
                {
                    "order_id": "order-newer",
                    "instrument_token": "NSE_FO|222",
                    "trading_symbol": "NIFTY26JUL25000PE",
                    "transaction_type": "SELL",
                    "order_type": "MARKET",
                    "product": "I",
                    "status": "rejected",
                    "quantity": 75,
                    "filled_quantity": 0,
                    "pending_quantity": 0,
                    "price": 0,
                    "average_price": 0,
                    "trigger_price": 0,
                    "order_timestamp": "2026-07-13 09:25:00",
                    "exchange_timestamp": "",
                    "status_message": "Margin exceeded",
                },
            ],
        }

    async def get_historical_trades(
        self,
        access_token: str,
        *,
        segment: str,
        start_date: str,
        end_date: str,
        page_number: int,
        page_size: int,
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "trade_id": "trade-older",
                    "instrument_token": "NSE_FO|111",
                    "symbol": "NIFTY26JUL25000CE",
                    "transaction_type": "BUY",
                    "quantity": 75,
                    "price": 120.0,
                    "amount": 9000.0,
                    "exchange": "NSE",
                    "segment": segment,
                    "option_type": "CE",
                    "strike_price": "25000",
                    "expiry": "2026-07-16",
                    "trade_date": "2026-07-10",
                },
                {
                    "trade_id": "trade-newer",
                    "instrument_token": "NSE_FO|222",
                    "symbol": "NIFTY26JUL25000PE",
                    "transaction_type": "SELL",
                    "quantity": 75,
                    "price": 90.0,
                    "amount": 6750.0,
                    "exchange": "NSE",
                    "segment": segment,
                    "option_type": "PE",
                    "strike_price": "25000",
                    "expiry": "2026-07-16",
                    "trade_date": "2026-07-12",
                },
            ],
            "meta_data": {
                "page": {
                    "page_number": page_number,
                    "page_size": page_size,
                    "total_records": 2,
                    "total_pages": 1,
                }
            },
        }

    async def place_gtt_order(
        self,
        access_token: str,
        order: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "gtt_order_ids": ["GTT-123"],
            },
            "echo": order,
        }

    async def modify_order(
        self,
        access_token: str,
        order: dict[str, Any],
    ) -> dict[str, Any]:
        if order["order_id"] == "order-fail":
            from app.core.exceptions import UpstoxApiError

            raise UpstoxApiError(
                "Order cannot be modified",
                status_code=400,
                upstox_code="UDAPI100041",
            )
        return {
            "status": "success",
            "data": {"order_id": order["order_id"]},
            "echo": order,
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
                "lot_size": 65,
                "freeze_quantity": 1755.0,
                "tick_size": 5.0,
            },
            {
                "name": "NIFTY",
                "expiry": "2026-07-23",
                "instrument_key": "NSE_FO|222",
                "trading_symbol": "NIFTY26JUL25000PE",
                "instrument_type": "PE",
                "underlying_symbol": "NIFTY",
                "strike_price": 25000,
                "lot_size": 65,
                "freeze_quantity": 1755.0,
                "tick_size": 5.0,
            },
        ]
        if expiry_date:
            contracts = [contract for contract in contracts if contract["expiry"] == expiry_date]
        return {"status": "success", "data": contracts}

    async def get_funds_and_margin(self, access_token: str) -> dict[str, Any]:
        if access_token == "funds-unavailable-token":
            # Mirrors Upstox's real nightly maintenance-window error (UDAPI100072) -- see
            # main_screen_service.summary()'s doc comment for why this must not take down the
            # whole bootstrap call.
            from app.core.exceptions import UpstoxApiError

            # Message/upstox_code here match what UpstoxService._build_api_error now extracts
            # from this exact real error shape (see that function's own test coverage) --
            # FakeUpstoxService stands in for UpstoxService entirely, so it must simulate that
            # extraction's result, not the raw response shape.
            raise UpstoxApiError(
                "The Funds service is accessible from 5:30 AM to 12:00 AM IST daily. Please "
                "try again during these service hours.",
                status_code=423,
                upstox_code="UDAPI100072",
                details={
                    "status": "error",
                    "errors": [
                        {
                            "errorCode": "UDAPI100072",
                            "message": (
                                "The Funds service is accessible from 5:30 AM to 12:00 AM IST "
                                "daily. Please try again during these service hours."
                            ),
                        }
                    ],
                },
            )
        return {
            "status": "success",
            "data": {
                "available_to_trade": {
                    "total": 99980.0,
                    "cash_available_to_trade": {
                        "total": 91980.0,
                        "cash": {
                            "opening_balance": 100000.0,
                            "added_today": 2000.0,
                            "withdrawn_today": -100.0,
                        },
                        "margin_used": {
                            "total": 9920.0,
                        },
                    },
                    "pledge_available_to_trade": {
                        "total": 8000.0,
                        "margin_used": {
                            "total": 80.0,
                        },
                    },
                },
            },
        }

    async def get_market_feed_authorize(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "authorized_redirect_uri": "wss://feed.test/socket?code=one-time",
            },
        }

    async def search_instruments(
        self,
        access_token: str,
        *,
        query: str,
        exchanges: str = "NSE,BSE",
        segments: str = "FO",
        instrument_types: str = "CE,PE",
        expiry: str = "current_month",
        atm_offset: int = 0,
        page_number: int = 1,
        records: int = 30,
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "name": "Nifty 50",
                    "exchange": "NSE",
                    "instrument_type": "CE",
                    "underlying_key": "NSE_INDEX|Nifty 50",
                    "underlying_type": "INDEX",
                    "underlying_symbol": "NIFTY",
                    "lot_size": 75,
                    "freeze_quantity": 1800.0,
                    "tick_size": 5.0,
                },
                {
                    "name": "Nifty 50",
                    "exchange": "NSE",
                    "instrument_type": "PE",
                    "underlying_key": "NSE_INDEX|Nifty 50",
                    "underlying_type": "INDEX",
                    "underlying_symbol": "NIFTY",
                    "lot_size": 75,
                    "freeze_quantity": 1800.0,
                    "tick_size": 5.0,
                },
                {
                    "name": "RELIANCE INDUSTRIES LTD",
                    "exchange": "NSE",
                    "instrument_type": "CE",
                    "underlying_key": "NSE_EQ|INE002A01018",
                    "underlying_type": "EQUITY",
                    "underlying_symbol": "RELIANCE",
                    "lot_size": 500,
                    "freeze_quantity": 10000.0,
                    "tick_size": 5.0,
                },
                {
                    "name": "Gold",
                    "exchange": "MCX",
                    "instrument_type": "CE",
                    "underlying_key": "MCX_FO|123",
                    "underlying_type": "COM",
                    "underlying_symbol": "GOLD",
                    "lot_size": 100,
                    "freeze_quantity": 10000.0,
                    "tick_size": 5.0,
                },
                {
                    "name": "Nifty Future",
                    "exchange": "NSE",
                    "instrument_type": "FUT",
                    "underlying_key": "NSE_INDEX|Nifty 50",
                    "underlying_type": "INDEX",
                    "underlying_symbol": "NIFTY",
                    "lot_size": 75,
                    "freeze_quantity": 1800.0,
                    "tick_size": 5.0,
                },
            ],
            "meta_data": {
                "page": {
                    "page_number": page_number,
                    "records": records,
                    "total_records": 5,
                    "total_pages": 1,
                }
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
    _SEARCH_CACHE.clear()
    instrument_rules_service._CACHE = _MasterCache(
        expires_at=9999999999,
        by_key={
            "NSE_FO|111": {
                "instrument_key": "NSE_FO|111",
                "lot_size": 75,
                "freeze_quantity": 1800,
                "tick_size": 5.0,
                "trading_symbol": "NIFTY26JUL25000CE",
            }
        },
    )
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
    """Return whether an Upstox token is present AND still actually valid."""
    client = _client(FakeTokenStore(token="upstox-token"))
    try:
        response = client.get("/api/auth/status", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"authenticated": True}


def test_auth_status_reports_expired_token_as_unauthenticated() -> None:
    """FIX: a stored token *file* can exist while Upstox itself has expired it overnight -- this
    must actually probe Upstox (via get_profile), not just check that a file is present.
    """
    client = _client(FakeTokenStore(token="expired-token"))
    try:
        response = client.get("/api/auth/status", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


def test_auth_status_reports_no_token_as_unauthenticated() -> None:
    """No stored token at all -- should short-circuit without calling Upstox."""
    client = _client(FakeTokenStore(token=None))
    try:
        response = client.get("/api/auth/status", headers={"X-API-Key": "mobile-secret"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


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
        "previous_close": 24950.0,
    }
    assert payload["expiries"] == ["2026-07-16", "2026-07-23"]
    assert payload["selected_expiry"] == "2026-07-16"
    assert payload["summary"] == {
        "opening_balance": 100000.0,
        "profit_loss": 400.0,
        "closing_balance": 102300.0,
        "available_margin": 99980.0,
        "margin_used": 10000.0,
        "payin_amount": 1900.0,
        "funds_unavailable_note": None,
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


def test_main_bootstrap_degrades_gracefully_when_funds_service_unavailable() -> None:
    """A funds/margin failure (e.g. Upstox's nightly maintenance window) must not take down the
    whole bootstrap call -- spot price, expiries, and positions are all independently available.
    """
    client = _client(FakeTokenStore(token="funds-unavailable-token"))
    try:
        response = client.get(
            "/api/main/bootstrap",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    # Unaffected -- these don't depend on the funds/margin call at all.
    assert payload["underlying"]["spot_price"] == 25050.0
    assert payload["expiries"] == ["2026-07-16", "2026-07-23"]
    assert payload["open_positions"] != []
    # Funds-derived fields degrade to 0 rather than the whole request failing, with a note
    # explaining why instead of silently looking like an empty/zero account.
    summary = payload["summary"]
    assert summary["opening_balance"] == 0.0
    assert summary["available_margin"] == 0.0
    assert summary["margin_used"] == 0.0
    assert summary["payin_amount"] == 0.0
    # profit_loss is unaffected since it comes from positions, not funds.
    assert summary["profit_loss"] == 400.0
    assert summary["funds_unavailable_note"] == (
        "The Funds service is accessible from 5:30 AM to 12:00 AM IST daily. Please try again "
        "during these service hours."
    )


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
            "lot_size": 65.0,
            "freeze_quantity": 1755.0,
            "tick_size": 0.05,
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
            {"instrument_key": "NSE_FO|111", "ltp": 125.0, "previous_close": 0.0},
            {"instrument_key": "NSE_FO|222", "ltp": 90.0, "previous_close": 0.0},
        ]
    }


def test_main_position_quotes_supports_global_instrument_keys() -> None:
    """The same generic quote call also works for Upstox's Global Instruments (e.g. S&P 500) --
    used to poll the toolbar's Global watchlist ticker, which has no WebSocket feed support.
    """
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/main/position-quotes?instrument_keys=GLOBAL_INDEX%7C%5EGSPC",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "positions": [
            {"instrument_key": "GLOBAL_INDEX|^GSPC", "ltp": 5555.5, "previous_close": 5540.0},
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
        "profit_loss": 400.0,
        "closing_balance": 102300.0,
        "available_margin": 99980.0,
        "margin_used": 10000.0,
        "payin_amount": 1900.0,
        "funds_unavailable_note": None,
    }


def test_get_funds_and_margin_returns_raw_upstox_payload() -> None:
    """Return the complete V3 funds-and-margin response without reshaping it."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/user/get-funds-and-margin",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "data": {
            "available_to_trade": {
                "total": 99980.0,
                "cash_available_to_trade": {
                    "total": 91980.0,
                    "cash": {
                        "opening_balance": 100000.0,
                        "added_today": 2000.0,
                        "withdrawn_today": -100.0,
                    },
                    "margin_used": {
                        "total": 9920.0,
                    },
                },
                "pledge_available_to_trade": {
                    "total": 8000.0,
                    "margin_used": {
                        "total": 80.0,
                    },
                },
            },
        },
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


def test_search_underlyings_returns_only_option_capable_indices_and_stocks() -> None:
    """Search screen returns deduped index/equity underlyings with F&O options."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/search/underlyings?query=nifty&limit=10",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "query": "nifty",
        "results": [
            {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "symbol": "NIFTY",
                "name": "Nifty 50",
                "underlying_type": "INDEX",
                "exchange": "NSE",
                "lot_size": 75.0,
                "freeze_quantity": 1800.0,
                "tick_size": 0.05,
            },
            {
                "instrument_key": "NSE_EQ|INE002A01018",
                "symbol": "RELIANCE",
                "name": "RELIANCE INDUSTRIES LTD",
                "underlying_type": "EQUITY",
                "exchange": "NSE",
                "lot_size": 500.0,
                "freeze_quantity": 10000.0,
                "tick_size": 0.05,
            },
        ],
        "page": {
            "page_number": 1,
            "records": 10,
            "total_records": 5,
            "total_pages": 1,
        },
    }


def test_search_underlyings_empty_query_returns_default_option_indices() -> None:
    """Empty search returns known index underlyings that provide options."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/search/underlyings?limit=2",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "query": "",
        "results": [
            {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "symbol": "NIFTY",
                "name": "Nifty 50",
                "underlying_type": "INDEX",
                "exchange": "NSE",
                "lot_size": 65.0,
                "freeze_quantity": 1755.0,
                "tick_size": 0.05,
            },
            {
                "instrument_key": "NSE_INDEX|Nifty Bank",
                "symbol": "BANKNIFTY",
                "name": "Nifty Bank",
                "underlying_type": "INDEX",
                "exchange": "NSE",
                "lot_size": 30.0,
                "freeze_quantity": 600.0,
                "tick_size": 0.05,
            },
        ],
        "page": {
            "page_number": 1,
            "records": 2,
            "total_records": 4,
            "total_pages": 2,
        },
    }


def test_order_history_today_returns_categorized_current_day_orders() -> None:
    """Order history defaults to current-day order book grouped by status."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/orders/history?scope=today&page_size=10",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "today"
    assert payload["source"] == "order_book"
    assert [order["id"] for order in payload["orders"]] == ["order-newer", "order-older"]
    assert [order["id"] for order in payload["categories"]["rejected"]] == ["order-newer"]
    assert [order["id"] for order in payload["categories"]["complete"]] == ["order-older"]
    assert payload["page"] == {
        "page_number": 1,
        "page_size": 10,
        "total_records": 2,
        "total_pages": 1,
    }


def test_order_history_all_returns_paginated_historical_trades() -> None:
    """All mode returns paginated historical executed trades newest first."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.get(
            "/api/orders/history"
            "?scope=all&page_number=1&page_size=50&start_date=2026-04-01&end_date=2026-07-13",
            headers={"X-API-Key": "mobile-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "all"
    assert payload["source"] == "historical_trades"
    assert "past executed trades" in payload["availability_note"]
    assert [order["id"] for order in payload["orders"]] == ["trade-newer", "trade-older"]
    assert [order["id"] for order in payload["categories"]["complete"]] == [
        "trade-newer",
        "trade-older",
    ]
    assert payload["filters"] == {
        "segment": "FO",
        "start_date": "2026-04-01",
        "end_date": "2026-07-13",
    }
    assert payload["page"] == {
        "page_number": 1,
        "page_size": 50,
        "total_records": 2,
        "total_pages": 1,
    }


def test_place_smart_bracket_order_submits_multi_leg_gtt() -> None:
    """Place a bracket-like order with client-provided GTT prices."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/smart-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 75,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.5,
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 118.0,
                "market_protection": -1,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["source"] == "upstox_gtt"
    assert payload["total_quantity"] == 75
    assert payload["slice_quantity"] == 75
    assert payload["slice_count"] == 1
    assert payload["slices"][0]["submitted_order"] == {
        "type": "MULTIPLE",
        "quantity": 75,
        "product": "I",
        "rules": [
            {
                "strategy": "ENTRY",
                "trigger_type": "IMMEDIATE",
                "trigger_price": 125.5,
                "market_protection": -1,
            },
            {
                "strategy": "TARGET",
                "trigger_type": "IMMEDIATE",
                "trigger_price": 140.0,
                "market_protection": -1,
            },
            {
                "strategy": "STOPLOSS",
                "trigger_type": "IMMEDIATE",
                "trigger_price": 118.0,
                "market_protection": -1,
            },
        ],
        "instrument_token": "NSE_FO|111",
        "transaction_type": "BUY",
    }
    assert payload["slices"][0]["upstox_response"]["data"] == {"gtt_order_ids": ["GTT-123"]}


def test_place_smart_bracket_order_slices_large_quantity() -> None:
    """Split large quantities so the client does not handle freeze slicing."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/smart-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 3750,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.5,
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 118.0,
                "slice_quantity": 1800,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_quantity"] == 3750
    assert payload["slice_quantity"] == 1800
    assert payload["slice_count"] == 3
    assert [item["quantity"] for item in payload["slices"]] == [1800, 1800, 150]
    assert [item["submitted_order"]["quantity"] for item in payload["slices"]] == [
        1800,
        1800,
        150,
    ]


def test_modify_orders_accepts_more_than_upstream_multi_order_limit() -> None:
    """Process every order without imposing a bulk request count limit."""
    client = _client(FakeTokenStore(token="stored-token"))
    orders = [
        {
            "order_id": f"order-{index}",
            "validity": "DAY",
            "price": 125.0 + index,
            "order_type": "LIMIT",
            "trigger_price": 0,
            "quantity": 75,
        }
        for index in range(25)
    ]
    try:
        response = client.put(
            "/api/orders/modify",
            headers={"X-API-Key": "mobile-secret"},
            json={"orders": orders},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["summary"] == {"total": 25, "success": 25, "failed": 0}
    assert [order["order_id"] for order in payload["orders"]] == [
        f"order-{index}" for index in range(25)
    ]
    assert payload["orders"][-1]["upstox_response"]["echo"] == orders[-1]


def test_modify_orders_continues_after_an_individual_failure() -> None:
    """Return partial results and still attempt orders after a rejected one."""
    client = _client(FakeTokenStore(token="stored-token"))
    orders = [
        {
            "order_id": order_id,
            "validity": "DAY",
            "price": 125.0,
            "order_type": "LIMIT",
            "trigger_price": 0,
        }
        for order_id in ("order-1", "order-fail", "order-3")
    ]
    try:
        response = client.put(
            "/api/orders/modify",
            headers={"X-API-Key": "mobile-secret"},
            json={"orders": orders},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "partial_success"
    assert payload["summary"] == {"total": 3, "success": 2, "failed": 1}
    assert [order["status"] for order in payload["orders"]] == [
        "success",
        "error",
        "success",
    ]
    assert payload["orders"][1]["error"]["upstox_code"] == "UDAPI100041"


def test_place_smart_bracket_order_rejects_invalid_lot_size() -> None:
    """Reject quantities that are not a multiple of the instrument lot size."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/smart-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 76,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.5,
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 118.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json() == {
        "status": "error",
        "message": "Quantity 76 must be a multiple of lot size 75",
    }


def test_place_smart_bracket_order_rejects_invalid_tick_size() -> None:
    """Reject prices that are not aligned to the instrument tick size."""
    client = _client(FakeTokenStore(token="stored-token"))
    try:
        response = client.post(
            "/api/orders/smart-bracket",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "instrument_key": "NSE_FO|111",
                "transaction_type": "BUY",
                "quantity": 75,
                "product": "I",
                "entry_trigger_type": "IMMEDIATE",
                "entry_trigger_price": 125.53,
                "target_trigger_price": 140.0,
                "stoploss_trigger_price": 118.0,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json() == {
        "status": "error",
        "message": "entry_trigger_price 125.53 must be a multiple of tick size 0.05",
    }

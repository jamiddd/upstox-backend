from __future__ import annotations

import json
from pathlib import Path

import anyio
import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import UpstoxApiError
from app.services.upstox_service import UpstoxService


def _settings() -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/token.enc"),
        upstox_api_base_url="https://api.test/v2",
        upstox_login_url="https://api.test/v2/login/authorization/dialog",
        upstox_token_url="https://api.test/v2/login/authorization/token",
    )


def test_build_login_url_encodes_oauth_parameters() -> None:
    """Build a valid Upstox OAuth authorization URL."""
    service = UpstoxService(_settings())

    login_url = service.build_login_url(state="mobile-state")

    assert login_url.startswith("https://api.test/v2/login/authorization/dialog?")
    assert "response_type=code" in login_url
    assert "client_id=api-key" in login_url
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fapi%2Fauth%2Fcallback" in login_url
    assert "state=mobile-state" in login_url


def test_exchange_code_for_token_posts_form_data() -> None:
    """Exchange an authorization code with Upstox token endpoint."""
    async def handler(request: httpx.Request) -> httpx.Response:
        body = (await request.aread()).decode("utf-8")
        assert request.method == "POST"
        assert request.url.path == "/v2/login/authorization/token"
        assert "code=auth-code" in body
        assert "grant_type=authorization_code" in body
        return httpx.Response(200, json={"access_token": "upstox-token"})

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            return await service.exchange_code_for_token("auth-code")

    payload = anyio.run(run)
    assert payload == {"access_token": "upstox-token"}


def test_get_ltp_sends_bearer_token() -> None:
    """Fetch LTP with the stored access token."""
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/market-quote/ltp"
        assert request.url.params["instrument_key"] == "NSE_EQ|INE848E01016"
        assert request.headers["Authorization"] == "Bearer upstox-token"
        return httpx.Response(200, json={"status": "success", "data": {}})

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            return await service.get_ltp("upstox-token", "NSE_EQ|INE848E01016")

    payload = anyio.run(run)
    assert payload == {"status": "success", "data": {}}


def test_get_order_book_uses_current_day_order_endpoint() -> None:
    """Fetch today's order book."""
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/order/retrieve-all"
        assert request.headers["Authorization"] == "Bearer upstox-token"
        return httpx.Response(200, json={"status": "success", "data": []})

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            return await service.get_order_book("upstox-token")

    payload = anyio.run(run)
    assert payload == {"status": "success", "data": []}


def test_get_historical_trades_sends_pagination_and_date_range() -> None:
    """Fetch historical trade records with segment, dates, and page params."""
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/charges/historical-trades"
        assert request.url.params["segment"] == "FO"
        assert request.url.params["start_date"] == "2026-04-01"
        assert request.url.params["end_date"] == "2026-07-13"
        assert request.url.params["page_number"] == "2"
        assert request.url.params["page_size"] == "50"
        assert request.headers["Authorization"] == "Bearer upstox-token"
        return httpx.Response(200, json={"status": "success", "data": []})

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            return await service.get_historical_trades(
                "upstox-token",
                segment="FO",
                start_date="2026-04-01",
                end_date="2026-07-13",
                page_number=2,
                page_size=50,
            )

    payload = anyio.run(run)
    assert payload == {"status": "success", "data": []}


def test_place_gtt_order_posts_to_v3_gtt_endpoint() -> None:
    """Place a GTT order through the V3 endpoint."""
    order = {
        "type": "MULTIPLE",
        "quantity": 75,
        "product": "I",
        "rules": [
            {"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": 125.5},
            {"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": 140.0},
            {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": 118.0},
        ],
        "instrument_token": "NSE_FO|111",
        "transaction_type": "BUY",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v3/order/gtt/place"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Authorization"] == "Bearer upstox-token"
        assert json.loads((await request.aread()).decode("utf-8")) == order
        return httpx.Response(
            200,
            json={"status": "success", "data": {"gtt_order_ids": ["GTT-123"]}},
        )

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            return await service.place_gtt_order("upstox-token", order)

    payload = anyio.run(run)
    assert payload == {"status": "success", "data": {"gtt_order_ids": ["GTT-123"]}}


def test_modify_order_puts_to_v3_modify_endpoint() -> None:
    """Modify an open order through the current V3 endpoint."""
    order = {
        "order_id": "240108010918222",
        "validity": "DAY",
        "price": 126.5,
        "order_type": "LIMIT",
        "trigger_price": 0.0,
        "quantity": 75,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/v3/order/modify"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Authorization"] == "Bearer upstox-token"
        assert json.loads((await request.aread()).decode("utf-8")) == order
        return httpx.Response(
            200,
            json={"status": "success", "data": {"order_id": order["order_id"]}},
        )

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            return await service.modify_order("upstox-token", order)

    payload = anyio.run(run)
    assert payload == {
        "status": "success",
        "data": {"order_id": "240108010918222"},
    }


def test_get_option_contracts_sends_underlying_and_expiry() -> None:
    """Fetch option contracts with the selected underlying and expiry."""
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/option/contract"
        assert request.url.params["instrument_key"] == "NSE_INDEX|Nifty 50"
        assert request.url.params["expiry_date"] == "2026-07-16"
        assert request.headers["Authorization"] == "Bearer upstox-token"
        return httpx.Response(200, json={"status": "success", "data": []})

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            return await service.get_option_contracts(
                "upstox-token",
                "NSE_INDEX|Nifty 50",
                expiry_date="2026-07-16",
            )

    payload = anyio.run(run)
    assert payload == {"status": "success", "data": []}


def test_get_funds_and_margin_uses_v3_api() -> None:
    """Fetch funds from the V3 account endpoint."""
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v3/user/get-funds-and-margin"
        assert request.headers["Api-Version"] == "3.0"
        assert request.headers["Authorization"] == "Bearer upstox-token"
        return httpx.Response(200, json={"status": "success", "data": {}})

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            return await service.get_funds_and_margin("upstox-token")

    payload = anyio.run(run)
    assert payload == {"status": "success", "data": {}}


def test_get_market_feed_authorize_uses_v2_authorize_endpoint() -> None:
    """Fetch a one-time WebSocket authorization URL for market streaming."""
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/feed/market-data-feed/authorize"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Authorization"] == "Bearer upstox-token"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"authorized_redirect_uri": "wss://feed.test/socket"},
            },
        )

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            return await service.get_market_feed_authorize("upstox-token")

    payload = anyio.run(run)
    assert payload == {
        "status": "success",
        "data": {"authorized_redirect_uri": "wss://feed.test/socket"},
    }


def test_search_instruments_sends_fo_option_filters() -> None:
    """Search instruments with filters that find option-capable underlyings."""
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/instruments/search"
        assert request.url.params["query"] == "NIFTY"
        assert request.url.params["exchanges"] == "NSE,BSE"
        assert request.url.params["segments"] == "FO"
        assert request.url.params["instrument_types"] == "CE,PE"
        assert request.url.params["expiry"] == "current_month"
        assert request.url.params["atm_offset"] == "0"
        assert request.url.params["page_number"] == "1"
        assert request.url.params["records"] == "30"
        assert request.headers["Authorization"] == "Bearer upstox-token"
        return httpx.Response(200, json={"status": "success", "data": []})

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            return await service.search_instruments("upstox-token", query="NIFTY")

    payload = anyio.run(run)
    assert payload == {"status": "success", "data": []}


def test_upstox_errors_are_normalized() -> None:
    """Convert failed Upstox responses into the service error type."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": "UDAPI1087", "message": "Invalid instrument"},
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UpstoxService(_settings(), client=client)
            await service.get_quotes("upstox-token", "bad-key")

    with pytest.raises(UpstoxApiError) as exc_info:
        anyio.run(run)

    assert exc_info.value.status_code == 400
    assert exc_info.value.upstox_code == "UDAPI1087"
    assert exc_info.value.message == "Invalid instrument"

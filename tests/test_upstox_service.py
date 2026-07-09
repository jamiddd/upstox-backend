from __future__ import annotations

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

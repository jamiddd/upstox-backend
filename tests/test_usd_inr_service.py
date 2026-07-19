from __future__ import annotations

import anyio
import httpx

from app.services import usd_inr_service
from app.services.usd_inr_service import UsdInrService


def _yahoo_response(
    *,
    regular_market_price: float = 96.27,
    previous_close: float | None = None,
    chart_previous_close: float | None = 96.335,
) -> httpx.Response:
    meta: dict[str, object] = {"regularMarketPrice": regular_market_price}
    if previous_close is not None:
        meta["previousClose"] = previous_close
    if chart_previous_close is not None:
        meta["chartPreviousClose"] = chart_previous_close
    return httpx.Response(200, json={"chart": {"result": [{"meta": meta}]}})


def test_get_quote_parses_ltp_and_previous_close() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "USDINR=X" in str(request.url)
        assert request.headers.get("user-agent") == "Mozilla/5.0"
        return _yahoo_response(regular_market_price=96.27, previous_close=96.10, chart_previous_close=96.335)

    usd_inr_service._CACHE = {}

    async def run() -> dict[str, float] | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await UsdInrService(client=client).get_quote()

    quote = anyio.run(run)

    assert quote == {"ltp": 96.27, "previous_close": 96.10}


def test_get_quote_falls_back_to_chart_previous_close_when_previous_close_missing() -> None:
    """Confirmed live: meta.previousClose is sometimes absent with range=1d&interval=1d --
    chartPreviousClose is the reliable fallback."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return _yahoo_response(regular_market_price=96.27, previous_close=None, chart_previous_close=96.335)

    usd_inr_service._CACHE = {}

    async def run() -> dict[str, float] | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await UsdInrService(client=client).get_quote()

    quote = anyio.run(run)

    assert quote == {"ltp": 96.27, "previous_close": 96.335}


def test_get_quote_returns_none_on_http_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="rate limited")

    usd_inr_service._CACHE = {}

    async def run() -> dict[str, float] | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await UsdInrService(client=client).get_quote()

    assert anyio.run(run) is None


def test_get_quote_returns_none_on_malformed_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"chart": {"result": []}})

    usd_inr_service._CACHE = {}

    async def run() -> dict[str, float] | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await UsdInrService(client=client).get_quote()

    assert anyio.run(run) is None


def test_get_quote_caches_across_calls_within_ttl() -> None:
    """A second call within the TTL should not hit the transport again."""
    call_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _yahoo_response()

    usd_inr_service._CACHE = {}

    async def run() -> tuple[dict[str, float] | None, dict[str, float] | None]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UsdInrService(client=client)
            first = await service.get_quote()
            second = await service.get_quote()
            return first, second

    first, second = anyio.run(run)

    assert first == second
    assert call_count == 1

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.core.exceptions import UpstoxApiError


class UpstoxService:
    """Small HTTP wrapper around the Upstox REST API used by V1 routes."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.settings = settings
        self._client = client

    def build_login_url(self, *, state: Optional[str] = None) -> str:
        """Build the Upstox OAuth authorization URL for the mobile app."""
        self.settings.require_upstox_oauth()
        query = {
            "response_type": "code",
            "client_id": self.settings.upstox_api_key,
            "redirect_uri": self.settings.upstox_redirect_url,
        }
        if state:
            query["state"] = state
        return f"{self.settings.upstox_login_url}?{urlencode(query)}"

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        """Exchange an OAuth authorization code for an Upstox access token."""
        self.settings.require_upstox_oauth()
        response = await self._request(
            "POST",
            self.settings.upstox_token_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "code": code,
                "client_id": self.settings.upstox_api_key,
                "client_secret": self.settings.upstox_api_secret,
                "redirect_uri": self.settings.upstox_redirect_url,
                "grant_type": "authorization_code",
            },
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstoxApiError("Unexpected Upstox token response")
        return payload

    async def get_ltp(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        """Fetch last traded price snapshots for one or more instruments."""
        return await self._get_json(
            "/market-quote/ltp",
            access_token,
            params={"instrument_key": instrument_key},
        )

    async def get_quotes(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        """Fetch full market quote snapshots for one or more instruments."""
        return await self._get_json(
            "/market-quote/quotes",
            access_token,
            params={"instrument_key": instrument_key},
        )

    async def get_holdings(self, access_token: str) -> dict[str, Any]:
        """Fetch long-term holdings for the logged-in Upstox account."""
        return await self._get_json("/portfolio/long-term-holdings", access_token)

    async def get_positions(self, access_token: str) -> dict[str, Any]:
        """Fetch current trading positions for the logged-in Upstox account."""
        return await self._get_json("/portfolio/short-term-positions", access_token)

    async def get_order_book(self, access_token: str) -> dict[str, Any]:
        """Fetch the current day's order book."""
        return await self._get_json("/order/retrieve-all", access_token)

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
        """Fetch paginated historical trade records."""
        return await self._get_json(
            "/charges/historical-trades",
            access_token,
            params={
                "segment": segment,
                "start_date": start_date,
                "end_date": end_date,
                "page_number": str(page_number),
                "page_size": str(page_size),
            },
        )

    async def place_gtt_order(
        self,
        access_token: str,
        order: dict[str, Any],
    ) -> dict[str, Any]:
        """Place a V3 GTT order."""
        response = await self._request(
            "POST",
            f"{self.settings.upstox_api_v3_base_url}/order/gtt/place",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            json=order,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstoxApiError("Unexpected Upstox GTT order response")
        return payload

    async def get_option_contracts(
        self,
        access_token: str,
        instrument_key: str,
        *,
        expiry_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fetch option contracts for an underlying, optionally scoped to an expiry."""
        params = {"instrument_key": instrument_key}
        if expiry_date:
            params["expiry_date"] = expiry_date
        return await self._get_json("/option/contract", access_token, params=params)

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
        """Search Upstox instruments with filters suitable for F&O underlyings."""
        return await self._get_json(
            "/instruments/search",
            access_token,
            params={
                "query": query,
                "exchanges": exchanges,
                "segments": segments,
                "instrument_types": instrument_types,
                "expiry": expiry,
                "atm_offset": str(atm_offset),
                "page_number": str(page_number),
                "records": str(records),
            },
        )

    async def get_funds_and_margin(self, access_token: str) -> dict[str, Any]:
        """Fetch V3 funds and margin data for account summary."""
        response = await self._request(
            "GET",
            f"{self.settings.upstox_api_v3_base_url}/user/get-funds-and-margin",
            headers={
                "Accept": "application/json",
                "Api-Version": "3.0",
                "Authorization": f"Bearer {access_token}",
            },
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstoxApiError("Unexpected Upstox funds response")
        return payload

    async def get_market_feed_authorize(self, access_token: str) -> dict[str, Any]:
        """Fetch a one-time V3 market data WebSocket authorization URL."""
        response = await self._request(
            "GET",
            f"{self.settings.upstox_api_base_url}/feed/market-data-feed/authorize",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstoxApiError("Unexpected Upstox market feed authorization response")
        return payload

    async def _get_json(
        self,
        path: str,
        access_token: str,
        *,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"{self.settings.upstox_api_base_url}{path}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            params=params,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstoxApiError("Unexpected Upstox API response")
        return payload

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send an HTTP request and convert Upstox failures into service errors."""
        client = self._client
        if client is not None:
            response = await client.request(method, url, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=15.0) as scoped_client:
                response = await scoped_client.request(method, url, **kwargs)

        if response.status_code >= 400:
            raise self._build_api_error(response)
        return response

    @staticmethod
    def _build_api_error(response: httpx.Response) -> UpstoxApiError:
        """Normalize an Upstox error response into an exception."""
        try:
            payload = response.json()
        except ValueError:
            return UpstoxApiError(
                "Upstox request failed",
                status_code=response.status_code,
                details=response.text,
            )

        message = "Upstox request failed"
        upstox_code = None
        details: Optional[object] = payload
        if isinstance(payload, dict):
            message_value = payload.get("message") or payload.get("errors")
            code_value = payload.get("code") or payload.get("errorCode")
            if isinstance(message_value, str):
                message = message_value
            if isinstance(code_value, str):
                upstox_code = code_value

        return UpstoxApiError(
            message,
            status_code=response.status_code,
            upstox_code=upstox_code,
            details=details,
        )

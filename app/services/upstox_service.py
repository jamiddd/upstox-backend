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

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

    async def get_brokerage(
        self,
        access_token: str,
        *,
        instrument_key: str,
        quantity: int,
        product: str,
        transaction_type: str,
        price: float,
    ) -> dict[str, Any]:
        """Calculate Upstox's estimated charges for one proposed order.

        The public Upstox API calls the identifier ``instrument_token``. The rest of this
        backend consistently calls the same value ``instrument_key``, so the translation is
        deliberately kept here at the upstream boundary.
        """
        return await self._get_json(
            "/charges/brokerage",
            access_token,
            params={
                "instrument_token": instrument_key,
                "quantity": str(quantity),
                "product": product,
                "transaction_type": transaction_type,
                "price": str(price),
            },
        )

    async def get_profile(self, access_token: str) -> dict[str, Any]:
        """Fetch the logged-in Upstox user's profile -- the lightest authenticated call Upstox
        offers, used purely to confirm a stored token is still actually valid (Upstox access
        tokens expire nightly; the encrypted token *file* otherwise stays present until a fresh
        login overwrites it, so its mere existence doesn't mean it still works)."""
        return await self._get_json("/user/profile", access_token)

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

    async def place_market_order(
        self,
        access_token: str,
        *,
        instrument_key: str,
        transaction_type: str,
        quantity: int,
        product: str,
    ) -> dict[str, Any]:
        """Places an immediate market order via Place Order V3 -- unlike place_gtt_order (a
        conditional GTT rule, this app's only other order-placement path, used for every normal
        Buy/Sell tap), this fills right away at whatever the market gives. Used to actually flatten
        a position (see SmartOrderService.exit_all_positions), where a GTT rule would be too slow/
        indirect for something meant to happen right now.

        Only documented on the separate api-hft.upstox.com host (upstox_api_hft_base_url), not the
        regular v3 base URL the rest of this class's order endpoints use.
        """
        order = {
            "quantity": quantity,
            "product": product,
            "validity": "DAY",
            "price": 0,
            "instrument_token": instrument_key,
            "order_type": "MARKET",
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        response = await self._request(
            "POST",
            f"{self.settings.upstox_api_hft_base_url}/order/place",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            json=order,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstoxApiError("Unexpected Upstox place order response")
        return payload

    async def modify_order(
        self,
        access_token: str,
        order: dict[str, Any],
    ) -> dict[str, Any]:
        """Modify an open or pending order through the V3 endpoint."""
        response = await self._request(
            "PUT",
            f"{self.settings.upstox_api_v3_base_url}/order/modify",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            json=order,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstoxApiError("Unexpected Upstox modify order response")
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

    async def get_option_chain(
        self,
        access_token: str,
        instrument_key: str,
        *,
        expiry_date: str,
    ) -> dict[str, Any]:
        """Fetch the full per-strike option chain for an underlying + expiry -- unlike
        get_option_contracts (bare contract metadata only: instrument key/lot size/tick size),
        this returns live market_data (ltp/bid/ask/oi/volume) AND option_greeks
        (delta/gamma/theta/vega/iv) for both call_options and put_options at every strike, in one
        call. Used by MainScreenService.option_chain() to power the app's smart strike selector.
        """
        return await self._get_json(
            "/option/chain",
            access_token,
            params={"instrument_key": instrument_key, "expiry_date": expiry_date},
        )

    async def get_oi(
        self,
        access_token: str,
        instrument_key: str,
        *,
        expiry: str,
        date: str,
    ) -> dict[str, Any]:
        """Fetch aggregate and per-strike call/put open interest for a dated expiry."""
        return await self._get_json(
            "/market/oi",
            access_token,
            params={"instrument_key": instrument_key, "expiry": expiry, "date": date},
        )

    async def get_change_oi(
        self,
        access_token: str,
        instrument_key: str,
        *,
        expiry: str,
        date: str,
        interval: int,
    ) -> dict[str, Any]:
        """Fetch per-strike OI changes over the requested number of days."""
        return await self._get_json(
            "/market/change-oi",
            access_token,
            params={
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": date,
                "interval": str(interval),
            },
        )

    async def get_max_pain(
        self,
        access_token: str,
        instrument_key: str,
        *,
        expiry: str,
        date: str,
        bucket_interval: int,
    ) -> dict[str, Any]:
        """Fetch max pain plus its intraday history at the requested minute interval."""
        return await self._get_json(
            "/market/max-pain",
            access_token,
            params={
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": date,
                "bucket_interval": str(bucket_interval),
            },
        )

    async def get_pcr(
        self,
        access_token: str,
        instrument_key: str,
        *,
        expiry: str,
        date: str,
        bucket_interval: int,
    ) -> dict[str, Any]:
        """Fetch put-call ratio plus its intraday history at the requested minute interval."""
        return await self._get_json(
            "/market/pcr",
            access_token,
            params={
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": date,
                "bucket_interval": str(bucket_interval),
            },
        )

    async def get_historical_candle(
        self,
        access_token: str,
        instrument_key: str,
        *,
        unit: str,
        interval: str,
        to_date: str,
        from_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fetch completed-session candles via the V3 historical-candle endpoint.

        `unit` is one of "minutes"/"hours"/"days"/"weeks"/"months"; `interval` is the bar size
        within that unit (e.g. "5" for 5-minute candles). Only covers *completed* trading
        sessions up to and including `to_date` -- today's still-forming candles come from
        get_intraday_candle instead, since Upstox never includes the current session here.
        """
        segments = [instrument_key, unit, interval, to_date]
        if from_date:
            segments.append(from_date)
        return await self._get_v3_json(f"/historical-candle/{'/'.join(segments)}", access_token)

    async def get_intraday_candle(
        self,
        access_token: str,
        instrument_key: str,
        *,
        unit: str,
        interval: str,
    ) -> dict[str, Any]:
        """Fetch today's still-forming candles via the V3 intraday historical-candle endpoint --
        the only source for the current session's bars, since get_historical_candle only ever
        returns completed sessions.
        """
        return await self._get_v3_json(
            f"/historical-candle/intraday/{instrument_key}/{unit}/{interval}",
            access_token,
        )

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
            f"{self.settings.upstox_api_v3_base_url}/feed/market-data-feed/authorize",
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

    async def _get_v3_json(
        self,
        path: str,
        access_token: str,
        *,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Same shape as _get_json, but against the V3 base URL -- for endpoints (like
        historical-candle) that only exist under /v3, not the V2 base most other GETs use.
        """
        response = await self._request(
            "GET",
            f"{self.settings.upstox_api_v3_base_url}{path}",
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
        """Normalize an Upstox error response into an exception.

        FIX: Upstox's actual error envelope is `{"status": "error", "errors": [{"errorCode":
        "...", "message": "..."}]}` -- `errors` is a *list* of objects, not a plain string. The
        `isinstance(message_value, str)` check below only ever matched the (rarer) shape where
        `message`/`errors` is itself a string, so for the common list-of-objects shape `message`
        silently stayed the generic "Upstox request failed" fallback -- e.g. Upstox's real
        "Funds service is only available 5:30 AM - 12:00 AM IST" explanation was present in the
        response the whole time, just never extracted, so every caller (including what the app
        shows the user) only ever saw the useless generic fallback instead.
        """
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
            elif isinstance(message_value, list) and message_value:
                first_error = message_value[0]
                if isinstance(first_error, dict):
                    nested_message = first_error.get("message")
                    if isinstance(nested_message, str):
                        message = nested_message
                    nested_code = first_error.get("errorCode") or first_error.get("error_code")
                    if isinstance(nested_code, str):
                        code_value = nested_code
            if isinstance(code_value, str):
                upstox_code = code_value

        return UpstoxApiError(
            message,
            status_code=response.status_code,
            upstox_code=upstox_code,
            details=details,
        )

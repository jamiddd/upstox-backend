from __future__ import annotations

import asyncio
from typing import Any

from app.services.search_screen_service import SearchScreenService


class _FakeUpstoxService:
    def __init__(self) -> None:
        self.requested_underlying: str | None = None

    async def get_option_contracts(
        self,
        access_token: str,
        instrument_key: str,
        *,
        expiry_date: str | None = None,
    ) -> dict[str, Any]:
        self.requested_underlying = instrument_key
        return {
            "data": [
                {
                    "instrument_key": "NSE_FO|1",
                    "trading_symbol": "NIFTY26JUL25000CE",
                    "name": "NIFTY",
                    "instrument_type": "CE",
                    "underlying_symbol": "NIFTY",
                    "expiry": "2026-07-30",
                    "strike_price": 25000,
                },
                {
                    "instrument_key": "NSE_FO|2",
                    "trading_symbol": "NIFTY26AUG25000CE",
                    "name": "NIFTY",
                    "instrument_type": "CE",
                    "underlying_symbol": "NIFTY",
                    "expiry": "2026-08-27",
                    "strike_price": 25000,
                },
                {
                    "instrument_key": "NSE_FO|3",
                    "trading_symbol": "NIFTY26SEP25000PE",
                    "name": "NIFTY",
                    "instrument_type": "PE",
                    "underlying_symbol": "NIFTY",
                    "expiry": "2026-09-24",
                    "strike_price": 25000,
                },
            ]
        }


def test_search_contracts_returns_contracts_across_all_expiries() -> None:
    upstox = _FakeUpstoxService()

    response = asyncio.run(
        SearchScreenService(upstox).search_contracts(
            "access-token",
            query="NIFTY",
            limit=1,
        )
    )

    assert upstox.requested_underlying == "NSE_INDEX|Nifty 50"
    assert {item["expiry"] for item in response["results"]} == {
        "2026-07-30",
        "2026-08-27",
        "2026-09-24",
    }
    assert len(response["results"]) == 3

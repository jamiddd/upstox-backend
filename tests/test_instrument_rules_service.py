from __future__ import annotations

import gzip
import json
from pathlib import Path

import anyio
import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import AppConfigError
from app.services import instrument_rules_service
from app.services.instrument_rules_service import (
    InstrumentRulesService,
    validate_price,
    validate_quantity,
)


def _settings() -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/token.enc"),
        upstox_instrument_master_url="https://assets.test/complete.json.gz",
    )


@pytest.fixture(autouse=True)
def clear_instrument_rules_cache() -> None:
    instrument_rules_service._CACHE = None


def test_get_rules_loads_gzipped_instrument_master() -> None:
    """Read lot, freeze, and normalized tick size from Upstox BOD data."""
    payload = gzip.compress(
        json.dumps(
            [
                {
                    "instrument_key": "NSE_FO|111",
                    "lot_size": 75,
                    "freeze_quantity": 1800.0,
                    "tick_size": 5.0,
                    "trading_symbol": "NIFTY26JUL25000CE",
                }
            ]
        ).encode("utf-8")
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://assets.test/complete.json.gz"
        return httpx.Response(200, content=payload)

    async def run() -> object:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = InstrumentRulesService(_settings(), client=client, ttl_seconds=0)
            return await service.get_rules("NSE_FO|111")

    rules = anyio.run(run)
    assert rules.lot_size == 75
    assert rules.freeze_quantity == 1800
    assert rules.tick_size == 0.05
    assert rules.trading_symbol == "NIFTY26JUL25000CE"


def test_validate_quantity_rejects_non_lot_multiple() -> None:
    """Quantity must be a multiple of lot size."""
    async def run() -> object:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=gzip.compress(
                        json.dumps(
                            [
                                {
                                    "instrument_key": "NSE_FO|111",
                                    "lot_size": 75,
                                    "freeze_quantity": 1800,
                                    "tick_size": 5.0,
                                }
                            ]
                        ).encode("utf-8")
                    ),
                )
            )
        ) as client:
            service = InstrumentRulesService(_settings(), client=client, ttl_seconds=0)
            return await service.get_rules("NSE_FO|111")

    rules = anyio.run(run)

    with pytest.raises(AppConfigError, match="multiple of lot size 75"):
        validate_quantity(76, rules)


def test_validate_price_rejects_non_tick_multiple() -> None:
    """Order prices must align to tick size."""
    async def run() -> object:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=gzip.compress(
                        json.dumps(
                            [
                                {
                                    "instrument_key": "NSE_FO|111",
                                    "lot_size": 75,
                                    "freeze_quantity": 1800,
                                    "tick_size": 5.0,
                                }
                            ]
                        ).encode("utf-8")
                    ),
                )
            )
        ) as client:
            service = InstrumentRulesService(_settings(), client=client, ttl_seconds=0)
            return await service.get_rules("NSE_FO|111")

    rules = anyio.run(run)

    with pytest.raises(AppConfigError, match="multiple of tick size 0.05"):
        validate_price(125.53, rules, field_name="entry_trigger_price")

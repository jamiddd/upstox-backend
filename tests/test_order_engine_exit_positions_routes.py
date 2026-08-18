from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi.testclient import TestClient

from dataclasses import replace

from app.api.dependencies import (
    get_order_engine_ledger_store,
    get_order_engine_lot_tracker,
    get_token_store,
    get_upstox_service,
)
from app.core.config import Settings, get_settings
from app.main import app
from app.services import instrument_rules_service
from app.services.instrument_rules_service import _MasterCache
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker

_HEADERS = {"X-API-Key": "mobile-secret"}


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


class _FakeTokenStore:
    def __init__(self, *, token: Optional[str] = "stored-token") -> None:
        self.token = token

    def has_token(self) -> bool:
        return self.token is not None

    def load_access_token(self) -> str:
        return self.token  # type: ignore[return-value]


def _seed_instrument_rules_cache() -> None:
    """[flatten_positions] looks up freeze quantity per position -- NSE_FO|222's freeze quantity
    (5000) is well above its 150-quantity position so it never slices, keeping this file's
    single-order-per-position assertions valid."""
    instrument_rules_service._CACHE = _MasterCache(
        expires_at=9999999999,
        by_key={
            "NSE_FO|111": {
                "instrument_key": "NSE_FO|111",
                "lot_size": 75,
                "freeze_quantity": 1800,
                "tick_size": 5.0,
                "trading_symbol": "NIFTY26JUL25000CE",
            },
            "NSE_FO|222": {
                "instrument_key": "NSE_FO|222",
                "lot_size": 150,
                "freeze_quantity": 5000,
                "tick_size": 5.0,
                "trading_symbol": "NIFTY26JUL25000PE",
            },
        },
    )


class _ExitAllFakeUpstoxService:
    """Two open positions (one long, one short) plus one already-closed one that must be
    skipped, and one instrument whose market order deliberately fails -- mirrors
    tests/test_routes.py's own _ExitAllFakeUpstoxService fixture for the old
    /orders/exit-all route, applied here against the new /order-engine/exit-all route."""

    async def get_positions(self, access_token: str) -> dict[str, Any]:
        return {
            "status": "success",
            "data": [
                {
                    "instrument_token": "NSE_FO|111",
                    "trading_symbol": "NIFTY26JUL25000CE",
                    "quantity": 75,
                    "product": "I",
                },
                {
                    "instrument_token": "NSE_FO|222",
                    "trading_symbol": "NIFTY26JUL25000PE",
                    "quantity": -150,
                    "product": "I",
                },
                {
                    "instrument_token": "NSE_FO|closed",
                    "trading_symbol": "NIFTY26JUL24000PE",
                    "quantity": 0,
                    "product": "I",
                },
            ],
        }

    async def place_market_order(
        self,
        access_token: str,
        *,
        instrument_key: str,
        transaction_type: str,
        quantity: int,
        product: str,
    ) -> dict[str, Any]:
        if instrument_key == "NSE_FO|222":
            from app.core.exceptions import UpstoxApiError

            raise UpstoxApiError("Order rejected", status_code=400, upstox_code="UDAPI100041")
        return {"status": "success", "data": {"order_ids": [f"MKT-{instrument_key}"]}}


def test_exit_all_flattens_every_open_position(tmp_path) -> None:
    _seed_instrument_rules_cache()
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    app.dependency_overrides[get_upstox_service] = _ExitAllFakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: _FakeTokenStore()
    client = TestClient(app)
    try:
        response = client.post("/api/order-engine/exit-all", headers=_HEADERS)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["positions_found"] == 2
    results_by_key = {item["instrument_key"]: item for item in payload["results"]}
    assert results_by_key["NSE_FO|111"]["transaction_type"] == "SELL"
    assert results_by_key["NSE_FO|111"]["quantity"] == 75
    assert results_by_key["NSE_FO|111"]["status"] == "success"
    assert results_by_key["NSE_FO|222"]["transaction_type"] == "BUY"
    assert results_by_key["NSE_FO|222"]["quantity"] == 150
    assert results_by_key["NSE_FO|222"]["status"] == "error"


def test_exit_positions_closes_only_the_requested_subset(tmp_path) -> None:
    _seed_instrument_rules_cache()
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    app.dependency_overrides[get_upstox_service] = _ExitAllFakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: _FakeTokenStore()
    client = TestClient(app)
    try:
        response = client.post(
            "/api/order-engine/exit-positions",
            json={"instrument_keys": ["NSE_FO|111"]},
            headers=_HEADERS,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["positions_found"] == 1
    assert payload["results"][0]["instrument_key"] == "NSE_FO|111"
    assert payload["results"][0]["status"] == "success"


def test_exit_all_requires_mobile_api_key(tmp_path) -> None:
    _seed_instrument_rules_cache()
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    app.dependency_overrides[get_upstox_service] = _ExitAllFakeUpstoxService
    app.dependency_overrides[get_token_store] = lambda: _FakeTokenStore()
    client = TestClient(app)
    try:
        response = client.post("/api/order-engine/exit-all")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code in (401, 403)

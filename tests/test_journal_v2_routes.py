from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.dependencies import get_order_engine_ledger_store
from app.core.config import Settings, get_settings
from app.main import app
from app.services.order_engine_ledger_store import OrderEngineLedgerStore

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


def _client(ledger: OrderEngineLedgerStore) -> TestClient:
    settings = _settings()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    return TestClient(app)


def _closed_lot(ledger: OrderEngineLedgerStore, lot_id: str, *, realized_pnl: float) -> None:
    ledger.upsert_lot(
        lot_id=lot_id, instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=0,
        realized_pnl=realized_pnl, state="CLOSED",
    )


def test_list_trades_only_returns_closed_lots(tmp_path) -> None:
    ledger = OrderEngineLedgerStore(
        replace(_settings(), order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    _closed_lot(ledger, "lot-closed", realized_pnl=250.0)
    ledger.upsert_lot(
        lot_id="lot-open", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50,
        realized_pnl=0.0, state="OPEN",
    )
    client = _client(ledger)
    response = client.get("/api/journal/v2/trades", headers=_HEADERS)
    assert response.status_code == 200
    trades = response.json()["trades"]
    assert [trade["id"] for trade in trades] == ["lot-closed"]


def test_trade_detail_includes_constituent_orders(tmp_path) -> None:
    ledger = OrderEngineLedgerStore(
        replace(_settings(), order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    _closed_lot(ledger, "lot-1", realized_pnl=100.0)
    ledger.upsert_order(
        id="oh-1", broker_order_id="broker-1", exchange_order_id=None, idempotency_key="idem-1",
        order_tag="tag-1", instrument_key="NSE_FO|1", trading_symbol="NIFTY", transaction_type="BUY",
        product="I", order_type="MARKET", requested_quantity=50, requested_price=None,
        trigger_price=None, status="complete", status_message=None, average_price=100.0,
        filled_quantity=50, lot_id="lot-1", role="ENTRY",
    )
    client = _client(ledger)
    response = client.get("/api/journal/v2/trades/lot-1", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["trade"]["id"] == "lot-1"
    assert [order["broker_order_id"] for order in body["orders"]] == ["broker-1"]


def test_trade_detail_missing_returns_404(tmp_path) -> None:
    ledger = OrderEngineLedgerStore(
        replace(_settings(), order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    client = _client(ledger)
    response = client.get("/api/journal/v2/trades/nope", headers=_HEADERS)
    assert response.status_code == 404


def test_update_notes_on_a_lot(tmp_path) -> None:
    ledger = OrderEngineLedgerStore(
        replace(_settings(), order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    _closed_lot(ledger, "lot-1", realized_pnl=100.0)
    client = _client(ledger)
    response = client.patch(
        "/api/journal/v2/trades/lot-1/notes",
        headers=_HEADERS,
        json={"strategy_tag": "breakout", "followed_plan": True, "remarks": "clean entry"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["strategy_tag"] == "breakout"
    assert body["followed_plan"] is True
    assert body["remarks"] == "clean entry"


def test_create_manual_trade_and_update_its_notes(tmp_path) -> None:
    ledger = OrderEngineLedgerStore(
        replace(_settings(), order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    client = _client(ledger)
    create_response = client.post(
        "/api/journal/v2/trades",
        headers=_HEADERS,
        json={
            "instrument_key": "NSE_FO|2", "trading_symbol": "BANKNIFTY", "transaction_type": "SELL",
            "product": "I", "quantity": 25, "average_price": 500.0,
        },
    )
    assert create_response.status_code == 200
    manual_order_id = create_response.json()["id"]

    notes_response = client.patch(
        f"/api/journal/v2/trades/{manual_order_id}/notes",
        headers=_HEADERS,
        json={"mistake_reason": "chased the move"},
    )
    assert notes_response.status_code == 200
    assert notes_response.json()["mistake_reason"] == "chased the move"


def test_filter_options_and_analytics_summary_shape(tmp_path) -> None:
    ledger = OrderEngineLedgerStore(
        replace(_settings(), order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    _closed_lot(ledger, "lot-1", realized_pnl=100.0)
    ledger.update_lot_notes("lot-1", setup_type="breakout", strategy_tag="momentum")
    ledger.upsert_order(
        id="oh-1", broker_order_id="broker-1", exchange_order_id=None, idempotency_key=None,
        order_tag=None, instrument_key="NSE_FO|1", trading_symbol="NIFTY", transaction_type="BUY",
        product="I", order_type="MARKET", requested_quantity=50, requested_price=None,
        trigger_price=None, status="complete", status_message=None, average_price=100.0,
        filled_quantity=50, lot_id="lot-1", role="ENTRY",
    )
    client = _client(ledger)

    filters = client.get("/api/journal/v2/filter-options", headers=_HEADERS).json()
    assert filters["trading_symbols"] == ["NIFTY"]
    assert filters["setups"] == ["breakout"]
    assert filters["strategy_tags"] == ["momentum"]

    summary = client.get("/api/analytics/v2/summary", headers=_HEADERS).json()
    assert summary["trade_count"] == 1
    assert summary["gross_pnl"] == 100.0
    assert summary["net_pnl"] is None
    assert len(summary["weekday_breakdown"]) == 7

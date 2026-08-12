from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.dependencies import get_order_engine_ledger_store, get_order_engine_lot_tracker
from app.core.config import Settings, get_settings
from app.main import app
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


def _client(tmp_path: Path) -> TestClient:
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    # get_order_engine_lot_tracker normally reads the app-lifetime singleton off app.state (see
    # its own doc comment) -- TestClient(app) here never runs the real lifespan, so this override
    # supplies a tracker bound to the *same* ledger instance the test itself writes through,
    # exactly like the real singleton would be.
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    return TestClient(app)


def test_upsert_and_get_lot_round_trips(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.put(
            "/api/order-engine/ledger/lots",
            headers=_HEADERS,
            json={
                "lot_id": "lot-1",
                "instrument_key": "NSE_FO|1",
                "transaction_type": "BUY",
                "entry_price": 100.0,
                "entry_quantity": 50,
                "remaining_quantity": 50,
                "state": "OPEN",
                "target_price": 110.0,
                "stoploss_price": 90.0,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["lot"]["id"] == "lot-1"

        fetched = client.get("/api/order-engine/ledger/lots/lot-1", headers=_HEADERS)
        assert fetched.status_code == 200
        assert fetched.json()["lot"]["state"] == "OPEN"
    finally:
        app.dependency_overrides.clear()


def test_get_lot_404_when_missing(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.get("/api/order-engine/ledger/lots/nope", headers=_HEADERS)
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_upsert_trigger_rule_and_tighten_stop_loss(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        client.put(
            "/api/order-engine/ledger/lots",
            headers=_HEADERS,
            json={
                "lot_id": "lot-1",
                "instrument_key": "NSE_FO|1",
                "transaction_type": "BUY",
                "entry_quantity": 50,
                "remaining_quantity": 50,
                "state": "OPEN",
            },
        )
        upsert = client.put(
            "/api/order-engine/ledger/trigger-rules",
            headers=_HEADERS,
            json={
                "rule_id": "rule-sl",
                "lot_id": "lot-1",
                "instrument_key": "NSE_FO|1",
                "role": "STOP_LOSS",
                "state": "ARMED",
                "condition_op": "BELOW",
                "condition_value": 90.0,
                "sibling_rule_id": "rule-tp",
            },
        )
        assert upsert.status_code == 200, upsert.text

        tightened = client.put(
            "/api/order-engine/ledger/trigger-rules/rule-sl/tighten-stop-loss",
            headers=_HEADERS,
            json={"condition_value": 95.0},
        )
        assert tightened.status_code == 200, tightened.text
        rule = tightened.json()["trigger_rule"]
        assert rule["condition_value"] == 95.0
        # Everything else must survive untouched -- this is a value-only CAS.
        assert rule["state"] == "ARMED"
        assert rule["sibling_rule_id"] == "rule-tp"
    finally:
        app.dependency_overrides.clear()


def test_tighten_stop_loss_404_for_unknown_rule(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.put(
            "/api/order-engine/ledger/trigger-rules/unknown/tighten-stop-loss",
            headers=_HEADERS,
            json={"condition_value": 95.0},
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_ledger_routes_require_mobile_api_key(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.get("/api/order-engine/ledger/lots/lot-1")
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


def test_upsert_max_loss_epoch_defaults_to_absolute_mode(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.put(
            "/api/order-engine/ledger/max-loss-epoch",
            headers=_HEADERS,
            json={
                "opening_balance": 100000.0,
                "peak_equity": 100000.0,
                "threshold_x": 5000.0,
                "epoch_started_at": "2026-08-12T09:15:00+00:00",
            },
        )
        assert response.status_code == 200, response.text
        epoch = response.json()["epoch"]
        assert epoch["threshold_mode"] == "ABSOLUTE"
        assert epoch["threshold_x"] == 5000.0
    finally:
        app.dependency_overrides.clear()


def test_upsert_max_loss_epoch_accepts_percentage_mode(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.put(
            "/api/order-engine/ledger/max-loss-epoch",
            headers=_HEADERS,
            json={
                "opening_balance": 100000.0,
                "peak_equity": 100000.0,
                "threshold_x": 5.0,
                "threshold_mode": "PERCENTAGE",
                "epoch_started_at": "2026-08-12T09:15:00+00:00",
            },
        )
        assert response.status_code == 200, response.text
        epoch = response.json()["epoch"]
        assert epoch["threshold_mode"] == "PERCENTAGE"
        assert epoch["threshold_x"] == 5.0
    finally:
        app.dependency_overrides.clear()


def test_get_max_loss_epoch_404_when_none_started(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.get("/api/order-engine/ledger/max-loss-epoch", headers=_HEADERS)
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_get_max_loss_epoch_returns_what_was_last_upserted(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        client.put(
            "/api/order-engine/ledger/max-loss-epoch",
            headers=_HEADERS,
            json={
                "opening_balance": 100000.0,
                "peak_equity": 105000.0,
                "threshold_x": 5.0,
                "threshold_mode": "PERCENTAGE",
                "epoch_started_at": "2026-08-12T09:15:00+00:00",
            },
        )

        response = client.get("/api/order-engine/ledger/max-loss-epoch", headers=_HEADERS)

        assert response.status_code == 200, response.text
        epoch = response.json()["epoch"]
        assert epoch["opening_balance"] == 100000.0
        assert epoch["peak_equity"] == 105000.0
        assert epoch["threshold_mode"] == "PERCENTAGE"
    finally:
        app.dependency_overrides.clear()


def test_upsert_max_loss_epoch_rejects_a_non_positive_threshold(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.put(
            "/api/order-engine/ledger/max-loss-epoch",
            headers=_HEADERS,
            json={
                "opening_balance": 100000.0,
                "peak_equity": 100000.0,
                "threshold_x": 0.0,
                "epoch_started_at": "2026-08-12T09:15:00+00:00",
            },
        )
        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_pnl_summary_sums_realized_across_open_and_closed_lots(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        client.put(
            "/api/order-engine/ledger/lots",
            headers=_HEADERS,
            json={
                "lot_id": "lot-open", "instrument_key": "NSE_FO|1", "transaction_type": "BUY",
                "entry_price": 100.0, "entry_quantity": 50, "remaining_quantity": 50,
                "realized_pnl": 250.0, "state": "OPEN",
            },
        )
        client.put(
            "/api/order-engine/ledger/lots",
            headers=_HEADERS,
            json={
                "lot_id": "lot-closed", "instrument_key": "NSE_FO|2", "transaction_type": "SELL",
                "entry_price": 200.0, "entry_quantity": 10, "remaining_quantity": 0,
                "realized_pnl": -30.0, "state": "CLOSED",
            },
        )

        response = client.get("/api/order-engine/ledger/pnl-summary", headers=_HEADERS)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["realized_pnl"] == 220.0  # 250 + (-30), both open and closed count
        assert body["unrealized_pnl"] == 0.0  # no ticks seen by this fresh tracker
        assert body["open_lot_count"] == 1  # only lot-open
    finally:
        app.dependency_overrides.clear()


def test_pnl_summary_is_all_zero_with_no_lots_at_all(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.get("/api/order-engine/ledger/pnl-summary", headers=_HEADERS)

        assert response.status_code == 200, response.text
        assert response.json() == {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "open_lot_count": 0}
    finally:
        app.dependency_overrides.clear()

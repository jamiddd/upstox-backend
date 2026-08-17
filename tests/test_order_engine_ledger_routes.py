from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_order_engine_ledger_store,
    get_order_engine_lot_tracker,
    get_token_store,
    get_upstox_service,
)
from app.core.config import Settings, get_settings
from app.main import app
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker
from app.services.order_engine_order_service import derive_order_tag
from tests.test_order_engine_order_service import FakeUpstox

_HEADERS = {"X-API-Key": "mobile-secret"}


class _FakePlacementTokenStore:
    def load_access_token(self) -> str:
        return "token"


class _FakePlacementUpstox:
    """Minimal fake for `OrderEngineOrderService.place_order`'s own calls -- an empty order book
    (so `find_existing_order` finds nothing, forcing a fresh placement) plus a scripted
    `place_order` accept response."""

    async def get_order_book(self, access_token):
        return {"status": "success", "data": []}

    async def place_order(self, access_token, **kwargs):
        return {"status": "success", "data": {"order_id": "broker-entry-1"}}


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


def test_get_lot_returns_what_the_store_holds(tmp_path) -> None:
    """`PUT /ledger/lots` was removed in Part 5 (docs/ORDER_HISTORY_V2_DESIGN.md) -- the server is
    now the sole writer of `lots`, so this seeds directly through the store, same as
    `OrderHistoryRecorder` would."""
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    ledger.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50,
        realized_pnl=0.0, state="OPEN", target_price=110.0, stoploss_price=90.0,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    try:
        fetched = TestClient(app).get("/api/order-engine/ledger/lots/lot-1", headers=_HEADERS)
        assert fetched.status_code == 200
        assert fetched.json()["lot"]["state"] == "OPEN"
    finally:
        app.dependency_overrides.clear()


def test_put_ledger_lots_route_is_gone(tmp_path) -> None:
    """Part 5's explicit "remove outright" decision -- nothing should still be listening here."""
    client = _client(tmp_path)
    try:
        response = client.put(
            "/api/order-engine/ledger/lots",
            headers=_HEADERS,
            json={
                "lot_id": "lot-1", "instrument_key": "NSE_FO|1", "transaction_type": "BUY",
                "entry_quantity": 50, "remaining_quantity": 50, "state": "OPEN",
            },
        )
        assert response.status_code in (404, 405)
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
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    ledger.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=None, entry_quantity=50, remaining_quantity=50,
        realized_pnl=0.0, state="OPEN",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    client = TestClient(app)
    try:
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
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    ledger.upsert_lot(
        lot_id="lot-open", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50,
        realized_pnl=250.0, state="OPEN",
    )
    ledger.upsert_lot(
        lot_id="lot-closed", instrument_key="NSE_FO|2", transaction_type="SELL",
        entry_price=200.0, entry_quantity=10, remaining_quantity=0,
        realized_pnl=-30.0, state="CLOSED",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    client = TestClient(app)
    try:
        response = client.get("/api/order-engine/ledger/pnl-summary", headers=_HEADERS)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["realized_pnl"] == 220.0  # 250 + (-30), both open and closed count
        assert body["unrealized_pnl"] == 0.0  # no ticks seen by this fresh tracker
        assert body["open_lot_count"] == 1  # only lot-open
        # per_lot mirrors open_lot_count -- one row, falling back to entry_price (0 live pnl)
        # since no tick has reached this fresh tracker yet, same fallback total_live_pnl uses.
        assert body["per_lot"] == [
            {"lot_id": "lot-open", "instrument_key": "NSE_FO|1", "ltp": 100.0, "unrealized_pnl": 0.0},
        ]
    finally:
        app.dependency_overrides.clear()


def test_pnl_summary_per_lot_reflects_a_live_tick(tmp_path) -> None:
    """Regression, 2026-08-13: found live -- the home screen's per-lot row had nowhere to source a
    live number from, only ever the permanently-zero realized_pnl of a still-open lot. Confirms
    per_lot actually moves once a real tick reaches the tracker, not just the aggregate total."""
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    ledger.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50,
        realized_pnl=0.0, state="OPEN",
    )
    tracker = OrderEngineLotTracker(ledger)
    tracker.apply_tick("NSE_FO|1", 106.0)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: tracker
    try:
        response = TestClient(app).get("/api/order-engine/ledger/pnl-summary", headers=_HEADERS)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["unrealized_pnl"] == 300.0  # (106 - 100) * 50
        assert body["per_lot"] == [
            {"lot_id": "lot-1", "instrument_key": "NSE_FO|1", "ltp": 106.0, "unrealized_pnl": 300.0},
        ]
    finally:
        app.dependency_overrides.clear()


def test_pnl_summary_is_all_zero_with_no_lots_at_all(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.get("/api/order-engine/ledger/pnl-summary", headers=_HEADERS)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "realized_pnl": 0.0, "unrealized_pnl": 0.0, "open_lot_count": 0, "per_lot": [],
        }
    finally:
        app.dependency_overrides.clear()


def _seed_order(ledger: OrderEngineLedgerStore, **overrides) -> dict:
    base = dict(
        id="hist-1", broker_order_id="broker-1", exchange_order_id=None,
        idempotency_key=None, order_tag=None, instrument_key="NSE_FO|1",
        trading_symbol="NIFTY", transaction_type="BUY", product="I", order_type="MARKET",
        requested_quantity=50, requested_price=None, trigger_price=None, status="open",
        status_message=None, average_price=None, filled_quantity=0,
    )
    base.update(overrides)
    return ledger.upsert_order(**base)


def test_list_order_history_returns_newest_first(tmp_path) -> None:
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    _seed_order(ledger, id="hist-1", broker_order_id="broker-1")
    _seed_order(ledger, id="hist-2", broker_order_id="broker-2")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    try:
        response = TestClient(app).get("/api/order-engine/orders", headers=_HEADERS)

        assert response.status_code == 200, response.text
        body = response.json()
        assert [order["broker_order_id"] for order in body["orders"]] == ["broker-2", "broker-1"]
        assert body["next_cursor"] is None
    finally:
        app.dependency_overrides.clear()


def test_list_order_history_paginates_with_a_cursor(tmp_path) -> None:
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    for index in range(1, 4):
        _seed_order(ledger, id=f"hist-{index}", broker_order_id=f"broker-{index}")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    client = TestClient(app)
    try:
        first_page = client.get("/api/order-engine/orders?limit=2", headers=_HEADERS)
        assert first_page.status_code == 200, first_page.text
        first_body = first_page.json()
        assert [o["broker_order_id"] for o in first_body["orders"]] == ["broker-3", "broker-2"]
        assert first_body["next_cursor"] is not None

        second_page = client.get(
            f"/api/order-engine/orders?limit=2&before={first_body['next_cursor']}", headers=_HEADERS,
        )
        assert second_page.status_code == 200, second_page.text
        second_body = second_page.json()
        assert [o["broker_order_id"] for o in second_body["orders"]] == ["broker-1"]
        assert second_body["next_cursor"] is None
    finally:
        app.dependency_overrides.clear()


def test_list_order_history_filters_by_status(tmp_path) -> None:
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    _seed_order(ledger, id="hist-a", broker_order_id="broker-a", status="complete")
    _seed_order(ledger, id="hist-b", broker_order_id="broker-b", status="rejected")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    try:
        response = TestClient(app).get(
            "/api/order-engine/orders?status=rejected", headers=_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert [o["broker_order_id"] for o in response.json()["orders"]] == ["broker-b"]
    finally:
        app.dependency_overrides.clear()


def test_list_order_history_empty_result_shape(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.get("/api/order-engine/orders", headers=_HEADERS)
        assert response.status_code == 200, response.text
        assert response.json() == {"orders": [], "next_cursor": None}
    finally:
        app.dependency_overrides.clear()


def test_list_order_history_requires_mobile_api_key(tmp_path) -> None:
    client = _client(tmp_path)
    try:
        response = client.get("/api/order-engine/orders")
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


def test_placing_an_order_with_a_role_writes_a_placement_time_correlation_row(tmp_path) -> None:
    """Part 5's entry-correlation fix (docs/ORDER_HISTORY_V2_DESIGN.md): a caller that supplies
    `role` gets a `submitted` order_history row written synchronously, keyed by the broker's own
    order_id, carrying the idempotency_key the server would otherwise have no way to recover
    later (its tag is a one-way hash)."""
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    app.dependency_overrides[get_upstox_service] = lambda: _FakePlacementUpstox()
    app.dependency_overrides[get_token_store] = lambda: _FakePlacementTokenStore()
    client = TestClient(app)
    try:
        response = client.post(
            "/api/order-engine/orders",
            headers=_HEADERS,
            json={
                "idempotency_key": "idem-entry-1",
                "instrument_key": "NSE_FO|1",
                "transaction_type": "BUY",
                "quantity": 50,
                "role": "ENTRY",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["broker_order_id"] == "broker-entry-1"

        row = ledger.get_order_by_broker_order_id("broker-entry-1")
        assert row is not None
        assert row["status"] == "submitted"
        assert row["idempotency_key"] == "idem-entry-1"
        assert row["role"] == "ENTRY"
        assert row["lot_id"] is None
    finally:
        app.dependency_overrides.clear()


def test_placing_an_order_without_a_role_writes_no_correlation_row(tmp_path) -> None:
    """Backward-compatible default: a caller that doesn't set `role` (every pre-existing caller)
    is unaffected -- no order_history row is written from the placement route at all."""
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    app.dependency_overrides[get_upstox_service] = lambda: _FakePlacementUpstox()
    app.dependency_overrides[get_token_store] = lambda: _FakePlacementTokenStore()
    client = TestClient(app)
    try:
        response = client.post(
            "/api/order-engine/orders",
            headers=_HEADERS,
            json={
                "idempotency_key": "idem-exit-1",
                "instrument_key": "NSE_FO|1",
                "transaction_type": "SELL",
                "quantity": 50,
            },
        )
        assert response.status_code == 200, response.text
        assert ledger.get_order_by_broker_order_id("broker-entry-1") is None
    finally:
        app.dependency_overrides.clear()


def test_get_engine_health_is_none_before_anything_has_evaluated() -> None:
    """§6.4 Part B4 -- a fresh process (or one that's never evaluated a tick) reports `None`, not
    a fabricated timestamp; the client's own EngineHeartbeatMonitor treats that as "not enough
    data yet," never an immediate DEGRADED verdict."""
    import app.services.order_engine_trigger_evaluator as evaluator

    evaluator._last_evaluated_at = None  # isolate from whatever an earlier test may have stamped
    app.dependency_overrides[get_settings] = lambda: _settings()
    try:
        response = TestClient(app).get("/api/order-engine/engine-health", headers=_HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["last_evaluated_at"] is None
    finally:
        app.dependency_overrides.clear()


def test_get_engine_health_reports_the_evaluators_own_last_stamp() -> None:
    import app.services.order_engine_trigger_evaluator as evaluator
    from datetime import datetime, timezone

    evaluator._last_evaluated_at = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
    app.dependency_overrides[get_settings] = lambda: _settings()
    try:
        response = TestClient(app).get("/api/order-engine/engine-health", headers=_HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["last_evaluated_at"] == "2026-08-17T10:00:00+00:00"
    finally:
        evaluator._last_evaluated_at = None
        app.dependency_overrides.clear()


def test_get_ledger_armed_brackets_joins_the_lots_own_exit_shape(tmp_path) -> None:
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    ledger.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50,
        realized_pnl=0.0, state="OPEN", product="I",
    )
    ledger.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=90.0,
        sibling_rule_id="rule-tp",
    )
    # PLACED, not ARMED -- must not appear in the response.
    ledger.upsert_trigger_rule(
        rule_id="rule-tp", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="TARGET", state="PLACED", condition_op="ABOVE", condition_value=120.0,
        sibling_rule_id="rule-sl",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    try:
        response = TestClient(app).get("/api/order-engine/ledger/armed-brackets", headers=_HEADERS)
        assert response.status_code == 200, response.text
        brackets = response.json()["brackets"]
        assert len(brackets) == 1
        bracket = brackets[0]
        assert bracket["rule_id"] == "rule-sl"
        assert bracket["condition_op"] == "BELOW"
        assert bracket["condition_value"] == 90.0
        assert bracket["lot_transaction_type"] == "BUY"
        assert bracket["lot_remaining_quantity"] == 50
        assert bracket["lot_product"] == "I"
    finally:
        app.dependency_overrides.clear()


def _seeded_cancel_ledger(tmp_path: Path) -> OrderEngineLedgerStore:
    settings = _settings()
    ledger = OrderEngineLedgerStore(
        replace(settings, order_engine_ledger_database_path=tmp_path / "ledger.sqlite3"),
    )
    ledger.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50,
        realized_pnl=0.0, state="OPEN", product="I",
    )
    return ledger


def _cancel_route_client_overrides(settings, ledger, fake_upstox) -> None:
    """Every branch of `cancel_ledger_trigger_rule` still declares `get_upstox_service`/
    `get_token_store` as FastAPI `Depends` params (same posture every other route in this file
    uses) -- they're resolved before the handler body's own state-based branching runs, so even
    the pure-internal-CAS `ARMED` path needs a working override, not a missing one. Whether the
    fake broker was actually *called* is asserted separately per test via `fake_upstox`'s own
    call-recording lists."""
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    app.dependency_overrides[get_upstox_service] = lambda: fake_upstox
    app.dependency_overrides[get_token_store] = lambda: _FakePlacementTokenStore()


def test_cancel_trigger_rule_404_for_unknown_rule(tmp_path) -> None:
    # `get_upstox_service`/`get_token_store` are still resolved before the 404 check runs (see
    # `_cancel_route_client_overrides`'s own doc comment) -- a plain `_client(tmp_path)` (no such
    # override) would fail on dependency resolution, not reach the assertion below.
    settings = _settings()
    ledger = _seeded_cancel_ledger(tmp_path)
    _cancel_route_client_overrides(settings, ledger, FakeUpstox())
    try:
        response = TestClient(app).post(
            "/api/order-engine/ledger/trigger-rules/unknown/cancel", headers=_HEADERS,
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_cancel_armed_trigger_rule_is_a_pure_internal_cas(tmp_path) -> None:
    """The ARMED branch must never actually call the broker, even though a working
    `get_upstox_service` override exists (FastAPI resolves it regardless of which branch the
    handler body takes) -- asserted directly via `fake_upstox`'s own empty call list, not by
    omitting the override."""
    settings = _settings()
    ledger = _seeded_cancel_ledger(tmp_path)
    ledger.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=90.0,
        sibling_rule_id=None,
    )
    fake_upstox = FakeUpstox()
    _cancel_route_client_overrides(settings, ledger, fake_upstox)
    try:
        response = TestClient(app).post(
            "/api/order-engine/ledger/trigger-rules/rule-sl/cancel", headers=_HEADERS,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outcome"] == "cancelled_internally"
        assert body["trigger_rule"]["state"] == "CANCELLED"
        assert ledger.get_trigger_rule("rule-sl")["state"] == "CANCELLED"
        assert fake_upstox.cancel_order_calls == []
    finally:
        app.dependency_overrides.clear()


def test_cancel_placed_trigger_rule_calls_the_real_broker_cancel(tmp_path) -> None:
    settings = _settings()
    ledger = _seeded_cancel_ledger(tmp_path)
    ledger.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="PLACED", condition_op="BELOW", condition_value=90.0,
        sibling_rule_id=None,
    )
    fake_upstox = FakeUpstox(order_book_data=[
        {"order_id": "broker-order-1", "tag": derive_order_tag("rule-sl"), "status": "open"},
    ])
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    app.dependency_overrides[get_upstox_service] = lambda: fake_upstox
    app.dependency_overrides[get_token_store] = lambda: _FakePlacementTokenStore()
    try:
        response = TestClient(app).post(
            "/api/order-engine/ledger/trigger-rules/rule-sl/cancel", headers=_HEADERS,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outcome"] == "cancelled_on_broker"
        assert body["broker_status"] == "cancelled"
        assert body["trigger_rule"]["state"] == "CANCELLED"
        assert fake_upstox.cancel_order_calls == ["broker-order-1"]
        assert ledger.get_trigger_rule("rule-sl")["state"] == "CANCELLED"
    finally:
        app.dependency_overrides.clear()


def test_cancel_placed_trigger_rule_not_found_on_broker(tmp_path) -> None:
    """No matching order on today's broker book at all (already filled, or never really placed) --
    404-shaped "not found," never conflated with a genuine cancel failure. The local row is left
    exactly as it was; there's nothing confirmed to CAS off."""
    settings = _settings()
    ledger = _seeded_cancel_ledger(tmp_path)
    ledger.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="PLACED", condition_op="BELOW", condition_value=90.0,
        sibling_rule_id=None,
    )
    fake_upstox = FakeUpstox(order_book_data=[])
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    app.dependency_overrides[get_order_engine_lot_tracker] = lambda: OrderEngineLotTracker(ledger)
    app.dependency_overrides[get_upstox_service] = lambda: fake_upstox
    app.dependency_overrides[get_token_store] = lambda: _FakePlacementTokenStore()
    try:
        response = TestClient(app).post(
            "/api/order-engine/ledger/trigger-rules/rule-sl/cancel", headers=_HEADERS,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outcome"] == "not_found_on_broker"
        assert body["trigger_rule"] is None
        assert ledger.get_trigger_rule("rule-sl")["state"] == "PLACED"
    finally:
        app.dependency_overrides.clear()


def test_cancel_trigger_rule_rejected_for_terminal_or_mid_transition_states(tmp_path) -> None:
    settings = _settings()
    ledger = _seeded_cancel_ledger(tmp_path)
    for state in ("EVALUATING", "FIRING", "FAILED", "CANCELLED"):
        ledger.upsert_trigger_rule(
            rule_id=f"rule-{state}", lot_id="lot-1", instrument_key="NSE_FO|1",
            role="STOP_LOSS", state=state, condition_op="BELOW", condition_value=90.0,
            sibling_rule_id=None,
        )
    fake_upstox = FakeUpstox()
    _cancel_route_client_overrides(settings, ledger, fake_upstox)
    try:
        for state in ("EVALUATING", "FIRING", "FAILED", "CANCELLED"):
            response = TestClient(app).post(
                f"/api/order-engine/ledger/trigger-rules/rule-{state}/cancel", headers=_HEADERS,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["outcome"] == "rejected"
            assert state in body["reason"]
            # Untouched -- a rejected cancel must never silently mutate state.
            assert ledger.get_trigger_rule(f"rule-{state}")["state"] == state
    finally:
        app.dependency_overrides.clear()

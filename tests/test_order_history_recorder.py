from __future__ import annotations

from app.core.config import Settings
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_history_recorder import OrderHistoryRecorder


def _settings(tmp_path) -> Settings:
    return Settings(
        upstox_api_key="key",
        upstox_api_secret="secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile",
        token_encryption_key="test-key",
        token_store_path=tmp_path / "token.enc",
        order_engine_ledger_database_path=tmp_path / "order_engine_ledger.sqlite3",
    )


def _broker_order(**overrides) -> dict:
    base = dict(
        order_id="broker-1",
        exchange_order_id="exch-1",
        tag="tag123",
        instrument_token="NSE_FO|1",
        trading_symbol="NIFTY",
        transaction_type="BUY",
        product="I",
        order_type="MARKET",
        quantity=50,
        price=0,
        trigger_price=0,
        status="open",
        status_message=None,
        average_price=None,
        filled_quantity=0,
        order_timestamp="2026-08-16T09:15:00+05:30",
        exchange_timestamp=None,
    )
    base.update(overrides)
    return base


def test_record_order_snapshot_upserts_every_sighting_regardless_of_status(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)

    recorder.record_order_snapshot(_broker_order(status="open"), idempotency_key="rule-1")
    updated = recorder.record_order_snapshot(
        _broker_order(status="complete", average_price=100.0, filled_quantity=50),
        idempotency_key="rule-1",
    )

    assert updated["status"] == "complete"
    assert updated["average_price"] == 100.0
    assert store.get_order_by_broker_order_id("broker-1")["status"] == "complete"


def test_record_order_snapshot_requires_order_id(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)

    try:
        recorder.record_order_snapshot({"status": "open"})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_apply_fill_to_ledger_entry_creates_a_new_lot(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)

    broker_order = _broker_order(status="complete", average_price=100.0, filled_quantity=50)
    lot = recorder.apply_fill_to_ledger(broker_order, lot_id="lot-1", role="ENTRY")

    assert lot["id"] == "lot-1"
    assert lot["state"] == "OPEN"
    assert lot["entry_price"] == 100.0
    assert lot["entry_quantity"] == 50
    assert lot["remaining_quantity"] == 50


def test_apply_fill_to_ledger_entry_arms_the_bracket_on_a_fresh_lot(tmp_path) -> None:
    """§6.3 Part B2: a fresh entry fill carrying a bracket must arm it server-side in the same
    pass that creates the lot -- no client round trip required."""
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)

    broker_order = _broker_order(status="complete", average_price=100.0, filled_quantity=50)
    lot = recorder.apply_fill_to_ledger(
        broker_order, lot_id="lot-1", role="ENTRY",
        target_price=120.0, stoploss_price=90.0, trailing_gap=None,
    )

    assert lot["target_price"] == 120.0
    assert lot["stoploss_price"] == 90.0
    assert lot["target_rule_id"] is not None
    assert lot["stoploss_rule_id"] is not None
    armed = store.get_armed_trigger_rules_for_instrument("NSE_FO|1")
    assert {rule["role"] for rule in armed} == {"TARGET", "STOP_LOSS"}


def test_apply_fill_to_ledger_entry_does_not_rearm_an_already_open_lot(tmp_path) -> None:
    """A second entry order building on an already-open lot must not create a second, disconnected
    pair of trigger_rules -- arming only ever happens once, at lot creation."""
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)
    store.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50,
        realized_pnl=0.0, state="OPEN",
    )

    second_fill = _broker_order(
        order_id="broker-2", status="complete", average_price=110.0, filled_quantity=50,
    )
    lot = recorder.apply_fill_to_ledger(
        second_fill, lot_id="lot-1", role="ENTRY",
        target_price=200.0, stoploss_price=50.0, trailing_gap=None,
    )

    assert lot["target_rule_id"] is None  # never armed -- this fill hit the re-average branch
    assert store.get_armed_trigger_rules_for_instrument("NSE_FO|1") == []


def test_apply_fill_to_ledger_entry_reaverages_an_existing_open_lot(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)
    store.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50,
        realized_pnl=0.0, state="OPEN",
    )

    second_fill = _broker_order(
        order_id="broker-2", status="complete", average_price=110.0, filled_quantity=50,
    )
    lot = recorder.apply_fill_to_ledger(second_fill, lot_id="lot-1", role="ENTRY")

    assert lot["entry_quantity"] == 100
    assert lot["entry_price"] == 105.0  # (100*50 + 110*50) / 100
    assert lot["remaining_quantity"] == 100


def test_apply_fill_to_ledger_exit_decrements_and_applies_realized_pnl(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)
    store.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50,
        realized_pnl=0.0, state="OPEN",
    )

    exit_order = _broker_order(
        order_id="broker-exit", transaction_type="SELL", status="complete",
        average_price=110.0, filled_quantity=50,
    )
    lot = recorder.apply_fill_to_ledger(
        exit_order, lot_id="lot-1", role="EXIT", entry_transaction_type="BUY",
    )

    assert lot["remaining_quantity"] == 0
    assert lot["state"] == "CLOSED"
    assert lot["realized_pnl"] == 500.0  # (110-100) * 50


def test_apply_fill_to_ledger_partial_exit_leaves_lot_open(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)
    store.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50,
        realized_pnl=0.0, state="OPEN",
    )

    exit_order = _broker_order(
        order_id="broker-exit", transaction_type="SELL", status="complete",
        average_price=110.0, filled_quantity=20,
    )
    lot = recorder.apply_fill_to_ledger(
        exit_order, lot_id="lot-1", role="EXIT", entry_transaction_type="BUY",
    )

    assert lot["remaining_quantity"] == 30
    assert lot["state"] == "OPEN"
    assert lot["realized_pnl"] == 200.0  # (110-100) * 20


def test_apply_fill_to_ledger_manual_role_does_not_mutate_lots(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)

    broker_order = _broker_order(status="complete", average_price=100.0, filled_quantity=50)
    result = recorder.apply_fill_to_ledger(broker_order, lot_id=None, role="MANUAL")

    assert result is None
    assert store.get_all_lots() == []


def test_apply_fill_to_ledger_no_op_when_not_complete(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)

    broker_order = _broker_order(status="open", average_price=None, filled_quantity=0)
    result = recorder.apply_fill_to_ledger(broker_order, lot_id="lot-1", role="ENTRY")

    assert result is None
    assert store.get_all_lots() == []


def test_record_placement_writes_a_submitted_row_with_no_lot_yet(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)

    row = recorder.record_placement(
        broker_order_id="broker-entry",
        order_tag="tagentry",
        idempotency_key="idem-1",
        role="ENTRY",
        instrument_key="NSE_FO|1",
        transaction_type="BUY",
        product="I",
        order_type="MARKET",
        requested_quantity=50,
        requested_price=None,
        trigger_price=None,
    )

    assert row["broker_order_id"] == "broker-entry"
    assert row["status"] == "submitted"
    assert row["idempotency_key"] == "idem-1"
    assert row["role"] == "ENTRY"
    assert row["lot_id"] is None
    assert row["filled_quantity"] == 0
    assert store.get_all_lots() == []


def test_record_placement_then_snapshot_preserves_idempotency_key_and_role(tmp_path) -> None:
    """Mirrors the real flow: record_placement writes the correlation at placement time; a later
    record_order_snapshot (the WS-push-triggered re-fetch) must not blank those fields out."""
    store = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(store)
    recorder.record_placement(
        broker_order_id="broker-entry", order_tag="tagentry", idempotency_key="idem-1",
        role="ENTRY", instrument_key="NSE_FO|1", transaction_type="BUY", product="I",
        order_type="MARKET", requested_quantity=50, requested_price=None, trigger_price=None,
    )

    updated = recorder.record_order_snapshot(
        _broker_order(order_id="broker-entry", status="complete", average_price=100.0, filled_quantity=50),
    )

    assert updated["idempotency_key"] == "idem-1"
    assert updated["role"] == "ENTRY"
    assert updated["status"] == "complete"

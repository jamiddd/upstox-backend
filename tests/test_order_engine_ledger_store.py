from __future__ import annotations

from app.core.config import Settings
from app.services.order_engine_ledger_store import OrderEngineLedgerStore


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


def test_upsert_lot_inserts_then_updates_the_same_row(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))

    inserted = store.upsert_lot(
        lot_id="lot-1",
        instrument_key="NSE_FO|1",
        transaction_type="BUY",
        entry_price=100.0,
        entry_quantity=50,
        remaining_quantity=50,
        realized_pnl=0.0,
        state="OPEN",
        target_price=110.0,
        stoploss_price=90.0,
    )
    assert inserted["id"] == "lot-1"
    assert inserted["state"] == "OPEN"
    first_created_at = inserted["created_at"]

    updated = store.upsert_lot(
        lot_id="lot-1",
        instrument_key="NSE_FO|1",
        transaction_type="BUY",
        entry_price=100.0,
        entry_quantity=50,
        remaining_quantity=0,
        realized_pnl=500.0,
        state="CLOSED",
    )
    assert updated["id"] == "lot-1"
    assert updated["state"] == "CLOSED"
    assert updated["remaining_quantity"] == 0
    # created_at is preserved across an upsert -- this is a resend/update of the same lot, not a
    # new one, so its original creation timestamp must not move.
    assert updated["created_at"] == first_created_at

    # A resend of the same id must not create a second row.
    with store._connect() as connection:  # noqa: SLF001 -- test-only direct check
        count = connection.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
    assert count == 1


def test_get_open_lots_excludes_closed(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    store.upsert_lot(
        lot_id="open-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=10, remaining_quantity=10,
        realized_pnl=0.0, state="OPEN",
    )
    store.upsert_lot(
        lot_id="closed-1", instrument_key="NSE_FO|2", transaction_type="SELL",
        entry_price=200.0, entry_quantity=10, remaining_quantity=0,
        realized_pnl=50.0, state="CLOSED",
    )

    open_lots = store.get_open_lots()

    assert [lot["id"] for lot in open_lots] == ["open-1"]


def test_upsert_trigger_rule_and_get_armed(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    store.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=10, remaining_quantity=10,
        realized_pnl=0.0, state="OPEN",
    )

    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=90.0,
        sibling_rule_id="rule-tp",
    )
    store.upsert_trigger_rule(
        rule_id="rule-tp", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="TARGET", state="ARMED", condition_op="ABOVE", condition_value=110.0,
        sibling_rule_id="rule-sl",
    )

    armed = store.get_armed_trigger_rules()
    assert {rule["id"] for rule in armed} == {"rule-sl", "rule-tp"}

    # Firing one side should CAS it out of the armed set once the caller updates it.
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="FIRING", condition_op="BELOW", condition_value=90.0,
        sibling_rule_id="rule-tp",
    )
    armed_after = store.get_armed_trigger_rules()
    assert [rule["id"] for rule in armed_after] == ["rule-tp"]


def test_events_are_append_only_and_ordered(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    store.record_event(event_type="ARMED", lot_id="lot-1", rule_id="rule-sl", payload={"level": 90})
    store.record_event(event_type="FIRED", lot_id="lot-1", rule_id="rule-sl", payload={"ltp": 89.5})

    events = store.get_events_for_lot("lot-1")

    assert [event["event_type"] for event in events] == ["ARMED", "FIRED"]
    assert events[1]["rule_id"] == "rule-sl"


def test_get_lot_returns_none_when_missing(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    assert store.get_lot("nope") is None
    assert store.get_trigger_rule("nope") is None


def test_get_all_lots_includes_closed_lots(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    store.upsert_lot(
        lot_id="open-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=10, remaining_quantity=10, realized_pnl=0.0, state="OPEN",
    )
    store.upsert_lot(
        lot_id="closed-1", instrument_key="NSE_FO|2", transaction_type="SELL",
        entry_price=200.0, entry_quantity=10, remaining_quantity=0, realized_pnl=50.0, state="CLOSED",
    )

    all_lots = store.get_all_lots()

    assert {lot["id"] for lot in all_lots} == {"open-1", "closed-1"}


def test_max_loss_epoch_upsert_and_get_round_trips(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    assert store.get_max_loss_epoch() is None

    epoch = store.upsert_max_loss_epoch(
        opening_balance=100000.0, peak_equity=100000.0, threshold_x=5000.0,
        epoch_started_at="2026-08-11T09:15:00+00:00",
    )
    assert epoch["opening_balance"] == 100000.0
    assert epoch["peak_equity"] == 100000.0

    # A second upsert replaces the single row wholesale, never a second row.
    store.upsert_max_loss_epoch(
        opening_balance=105000.0, peak_equity=108000.0, threshold_x=5000.0,
        epoch_started_at="2026-08-11T10:00:00+00:00",
    )
    updated = store.get_max_loss_epoch()
    assert updated["opening_balance"] == 105000.0
    assert updated["peak_equity"] == 108000.0

    with store._connect() as connection:  # noqa: SLF001 -- test-only direct check
        count = connection.execute("SELECT COUNT(*) FROM max_loss_epochs").fetchone()[0]
    assert count == 1


def test_max_loss_epoch_threshold_mode_defaults_to_absolute(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))

    epoch = store.upsert_max_loss_epoch(
        opening_balance=100000.0, peak_equity=100000.0, threshold_x=5000.0,
        epoch_started_at="2026-08-12T09:15:00+00:00",
    )

    assert epoch["threshold_mode"] == "ABSOLUTE"


def test_max_loss_epoch_persists_percentage_threshold_mode(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))

    epoch = store.upsert_max_loss_epoch(
        opening_balance=100000.0, peak_equity=100000.0, threshold_x=5.0,
        threshold_mode="PERCENTAGE", epoch_started_at="2026-08-12T09:15:00+00:00",
    )

    assert epoch["threshold_mode"] == "PERCENTAGE"
    assert epoch["threshold_x"] == 5.0
    assert store.get_max_loss_epoch()["threshold_mode"] == "PERCENTAGE"

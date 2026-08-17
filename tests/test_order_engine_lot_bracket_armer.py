from __future__ import annotations

from app.core.config import Settings
from app.services.order_engine_lot_bracket_armer import arm_lot_bracket
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


def _fresh_lot(store, transaction_type="BUY"):
    return store.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type=transaction_type,
        entry_price=100.0, entry_quantity=50, remaining_quantity=50, realized_pnl=0.0, state="OPEN",
    )


def test_arm_lot_bracket_is_a_no_op_with_no_prices(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    lot = _fresh_lot(store)

    updated = arm_lot_bracket(store, lot, target_price=None, stoploss_price=None, trailing_gap=None)

    assert updated == lot
    assert store.get_armed_trigger_rules_for_instrument("NSE_FO|1") == []


def test_arm_lot_bracket_on_a_long_lot_uses_the_right_directions_and_links_siblings(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    lot = _fresh_lot(store, transaction_type="BUY")

    updated = arm_lot_bracket(store, lot, target_price=120.0, stoploss_price=90.0, trailing_gap=None)

    assert updated["target_price"] == 120.0
    assert updated["stoploss_price"] == 90.0
    target_rule = store.get_trigger_rule(updated["target_rule_id"])
    stoploss_rule = store.get_trigger_rule(updated["stoploss_rule_id"])
    assert target_rule["condition_op"] == "ABOVE"
    assert target_rule["condition_value"] == 120.0
    assert target_rule["state"] == "ARMED"
    assert stoploss_rule["condition_op"] == "BELOW"
    assert stoploss_rule["condition_value"] == 90.0
    # OCO siblings, cross-linked both ways
    assert target_rule["sibling_rule_id"] == stoploss_rule["id"]
    assert stoploss_rule["sibling_rule_id"] == target_rule["id"]


def test_arm_lot_bracket_on_a_short_lot_reverses_the_directions(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    lot = _fresh_lot(store, transaction_type="SELL")

    updated = arm_lot_bracket(store, lot, target_price=80.0, stoploss_price=110.0, trailing_gap=None)

    target_rule = store.get_trigger_rule(updated["target_rule_id"])
    stoploss_rule = store.get_trigger_rule(updated["stoploss_rule_id"])
    assert target_rule["condition_op"] == "BELOW"
    assert stoploss_rule["condition_op"] == "ABOVE"


def test_arm_lot_bracket_with_only_a_stop_loss_leaves_no_sibling_to_cancel(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    lot = _fresh_lot(store)

    updated = arm_lot_bracket(store, lot, target_price=None, stoploss_price=90.0, trailing_gap=5.0)

    assert updated["target_rule_id"] is None
    stoploss_rule = store.get_trigger_rule(updated["stoploss_rule_id"])
    assert stoploss_rule["sibling_rule_id"] is None
    assert updated["trailing_gap"] == 5.0

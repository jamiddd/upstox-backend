from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.exit_reconciliation_checker import ExitReconciliationChecker
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


class FakeUpstox:
    def __init__(self, order_book_data=None) -> None:
        self.order_book_data = order_book_data if order_book_data is not None else []

    async def get_order_book(self, access_token):
        return {"status": "success", "data": self.order_book_data}


def _long_lot(store, lot_id="lot-1", entry_price=100.0, remaining_quantity=0, realized_pnl=0.0):
    store.upsert_lot(
        lot_id=lot_id, instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=entry_price, entry_quantity=50, remaining_quantity=remaining_quantity,
        realized_pnl=realized_pnl, state="CLOSED" if remaining_quantity == 0 else "PARTIALLY_CLOSED",
    )


@pytest.mark.anyio
async def test_check_lot_matches_when_ledger_agrees_with_broker_truth(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    # (105-100)*50 = 250 -- ledger's realized_pnl set to match broker truth exactly.
    _long_lot(store, entry_price=100.0, realized_pnl=250.0)
    upstox = FakeUpstox(order_book_data=[
        {"order_id": "exit-1", "average_price": 105.0, "filled_quantity": 50},
    ])
    checker = ExitReconciliationChecker(store, upstox)

    result = await checker.check_lot("token", "lot-1", "exit-1")

    assert result.outcome == "matched"
    assert result.broker_realized_pnl == 250.0
    assert result.ledger_realized_pnl == 250.0
    events = store.get_events_for_lot("lot-1")
    assert events[-1]["event_type"] == "RECONCILIATION_MATCHED"


@pytest.mark.anyio
async def test_check_lot_flags_a_server_vs_client_mismatch(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _long_lot(store, entry_price=100.0, realized_pnl=999.0)  # client reported something wrong
    upstox = FakeUpstox(order_book_data=[
        {"order_id": "exit-1", "average_price": 105.0, "filled_quantity": 50},
    ])
    checker = ExitReconciliationChecker(store, upstox)

    result = await checker.check_lot("token", "lot-1", "exit-1")

    assert result.outcome == "server_vs_client_mismatch"
    assert result.is_mismatch
    assert result.broker_realized_pnl == 250.0
    assert result.ledger_realized_pnl == 999.0
    events = store.get_events_for_lot("lot-1")
    assert events[-1]["event_type"] == "RECONCILIATION_MISMATCH"


@pytest.mark.anyio
async def test_check_lot_handles_a_short_lot_sign_correctly(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    store.upsert_lot(
        lot_id="lot-short", instrument_key="NSE_FO|1", transaction_type="SELL",
        entry_price=100.0, entry_quantity=50, remaining_quantity=0,
        realized_pnl=250.0, state="CLOSED",  # (100-95)*50 = 250, since sign=-1
    )
    upstox = FakeUpstox(order_book_data=[
        {"order_id": "exit-1", "average_price": 95.0, "filled_quantity": 50},
    ])
    checker = ExitReconciliationChecker(store, upstox)

    result = await checker.check_lot("token", "lot-short", "exit-1")

    assert result.outcome == "matched"
    assert result.broker_realized_pnl == 250.0


@pytest.mark.anyio
async def test_check_lot_reports_lot_not_found(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    upstox = FakeUpstox()
    checker = ExitReconciliationChecker(store, upstox)

    result = await checker.check_lot("token", "no-such-lot", "exit-1")

    assert result.outcome == "lot_not_found"


@pytest.mark.anyio
async def test_check_lot_reports_broker_order_not_found(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _long_lot(store, entry_price=100.0, realized_pnl=250.0)
    upstox = FakeUpstox(order_book_data=[])  # exit order not in the book at all
    checker = ExitReconciliationChecker(store, upstox)

    result = await checker.check_lot("token", "lot-1", "exit-missing")

    assert result.outcome == "broker_order_not_found"


@pytest.mark.anyio
async def test_check_lot_reports_lot_not_found_when_entry_price_never_recorded(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    store.upsert_lot(
        lot_id="lot-no-entry", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=None, entry_quantity=50, remaining_quantity=0,
        realized_pnl=0.0, state="CLOSED",
    )
    upstox = FakeUpstox(order_book_data=[
        {"order_id": "exit-1", "average_price": 105.0, "filled_quantity": 50},
    ])
    checker = ExitReconciliationChecker(store, upstox)

    result = await checker.check_lot("token", "lot-no-entry", "exit-1")

    assert result.outcome == "lot_not_found"
    assert result.detail is not None

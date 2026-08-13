from __future__ import annotations

from app.core.config import Settings
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker


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


def _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY", entry_price=100.0, remaining_quantity=50):
    store.upsert_lot(
        lot_id=lot_id, instrument_key=instrument_key, transaction_type=transaction_type,
        entry_price=entry_price, entry_quantity=remaining_quantity, remaining_quantity=remaining_quantity,
        realized_pnl=0.0, state="OPEN", target_price=110.0, stoploss_price=90.0,
    )


def test_apply_tick_computes_live_pnl_for_a_long_lot(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store)
    tracker = OrderEngineLotTracker(store)

    statuses = tracker.apply_tick("NSE_FO|1", 105.0)

    assert len(statuses) == 1
    status = statuses[0]
    assert status.lot_id == "lot-1"
    assert status.ltp == 105.0
    assert status.live_pnl == (105.0 - 100.0) * 50  # long: profits as price rises
    assert status.target_price == 110.0
    assert status.stoploss_price == 90.0


def test_apply_tick_computes_live_pnl_for_a_short_lot(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store, lot_id="lot-short", transaction_type="SELL", entry_price=100.0, remaining_quantity=50)
    tracker = OrderEngineLotTracker(store)

    statuses = tracker.apply_tick("NSE_FO|1", 95.0)

    assert statuses[0].live_pnl == (95.0 - 100.0) * 50 * -1.0  # short: profits as price falls
    assert statuses[0].live_pnl == 250.0


def test_apply_tick_returns_empty_for_instrument_with_no_open_lot(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    tracker = OrderEngineLotTracker(store)

    assert tracker.apply_tick("NSE_FO|unrelated", 100.0) == []


def test_apply_tick_returns_empty_when_ltp_is_none(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store)
    tracker = OrderEngineLotTracker(store)

    assert tracker.apply_tick("NSE_FO|1", None) == []


def test_lot_with_no_entry_price_yet_contributes_zero(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store, entry_price=None)
    tracker = OrderEngineLotTracker(store)

    statuses = tracker.apply_tick("NSE_FO|1", 105.0)

    assert statuses[0].live_pnl == 0.0


def test_total_live_pnl_sums_across_instruments_using_last_seen_tick(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", entry_price=100.0, remaining_quantity=50)
    _open_lot(store, lot_id="lot-2", instrument_key="NSE_FO|2", entry_price=200.0, remaining_quantity=10)
    tracker = OrderEngineLotTracker(store)

    tracker.apply_tick("NSE_FO|1", 110.0)  # +10 * 50 = 500
    tracker.apply_tick("NSE_FO|2", 190.0)  # -10 * 10 = -100

    assert tracker.total_live_pnl() == 400.0


def test_total_live_pnl_is_zero_for_an_instrument_with_no_tick_seen_yet(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store, entry_price=100.0, remaining_quantity=50)
    tracker = OrderEngineLotTracker(store)

    # No apply_tick call at all -- falls back to entry_price, i.e. zero live P&L, never a stale
    # foreign price.
    assert tracker.total_live_pnl() == 0.0


def test_per_lot_live_pnl_returns_one_status_per_open_lot(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", entry_price=100.0, remaining_quantity=50)
    _open_lot(store, lot_id="lot-2", instrument_key="NSE_FO|2", entry_price=200.0, remaining_quantity=10)
    tracker = OrderEngineLotTracker(store)
    tracker.apply_tick("NSE_FO|1", 110.0)
    tracker.apply_tick("NSE_FO|2", 190.0)

    statuses = {status.lot_id: status for status in tracker.per_lot_live_pnl()}

    assert len(statuses) == 2
    assert statuses["lot-1"].live_pnl == 500.0
    assert statuses["lot-1"].ltp == 110.0
    assert statuses["lot-2"].live_pnl == -100.0
    assert statuses["lot-2"].ltp == 190.0


def test_per_lot_live_pnl_falls_back_to_entry_price_with_no_tick_yet(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store, entry_price=100.0, remaining_quantity=50)
    tracker = OrderEngineLotTracker(store)

    statuses = tracker.per_lot_live_pnl()

    assert len(statuses) == 1
    assert statuses[0].ltp == 100.0
    assert statuses[0].live_pnl == 0.0


def test_instrument_keys_returns_every_open_lots_instrument(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1")
    _open_lot(store, lot_id="lot-2", instrument_key="NSE_FO|2")
    store.upsert_lot(
        lot_id="lot-closed", instrument_key="NSE_FO|3", transaction_type="BUY",
        entry_price=100.0, entry_quantity=10, remaining_quantity=0, realized_pnl=10.0, state="CLOSED",
    )
    tracker = OrderEngineLotTracker(store)

    assert tracker.instrument_keys() == {"NSE_FO|1", "NSE_FO|2"}


def test_open_lots_without_recent_tick_reports_only_the_un_ticked_ones(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store, lot_id="lot-ticked", instrument_key="NSE_FO|1")
    _open_lot(store, lot_id="lot-un-ticked", instrument_key="NSE_FO|2")
    tracker = OrderEngineLotTracker(store)

    tracker.apply_tick("NSE_FO|1", 110.0)

    without_a_tick = tracker.open_lots_without_recent_tick()
    assert [lot["id"] for lot in without_a_tick] == ["lot-un-ticked"]


def test_open_lots_without_recent_tick_is_empty_once_every_open_lot_has_ticked(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1")
    tracker = OrderEngineLotTracker(store)

    tracker.apply_tick("NSE_FO|1", 110.0)

    assert tracker.open_lots_without_recent_tick() == []


def test_open_lots_without_recent_tick_excludes_closed_lots(tmp_path) -> None:
    store = OrderEngineLedgerStore(_settings(tmp_path))
    store.upsert_lot(
        lot_id="lot-closed", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=10, remaining_quantity=0, realized_pnl=10.0, state="CLOSED",
    )
    tracker = OrderEngineLotTracker(store)

    assert tracker.open_lots_without_recent_tick() == []

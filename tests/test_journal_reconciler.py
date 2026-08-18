from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.journal_reconciler import JournalReconciler
from app.services.journal_store import JournalStore

pytestmark = pytest.mark.anyio


class _Token:
    def __init__(self, valid=True):
        self.valid = valid
    def has_token(self):
        return self.valid
    def load_access_token(self):
        return "token"


class _LedgerStore:
    """Fake `OrderEngineLedgerStore` -- resolves [known_order_ids] as if the order engine placed/
    managed them (an `order_history` row with a non-`None` role), everything else as unscoped
    (broker-placed outside the app), same distinction [JournalReconciler]'s own scoping check uses."""
    def __init__(self, known_order_ids=("order-1", "order-2")):
        self.known_order_ids = set(known_order_ids)
    def get_order_by_broker_order_id(self, broker_order_id):
        return {"role": "ENTRY"} if broker_order_id in self.known_order_ids else None


class _Upstox:
    async def get_trades_for_day(self, token):
        return {"data": [
            {
                "trade_id": "fill-1", "order_id": "order-1",
                "instrument_token": "NSE_FO|1", "trading_symbol": "NIFTYCE",
                "transaction_type": "BUY", "quantity": 75, "average_price": 100,
                "exchange_timestamp": "2026-07-27 09:30:00",
            },
            {
                "trade_id": "fill-2", "order_id": "order-2",
                "instrument_token": "NSE_FO|1", "trading_symbol": "NIFTYCE",
                "transaction_type": "SELL", "quantity": 75, "average_price": 110,
                "exchange_timestamp": "2026-07-27 09:31:00",
            },
        ]}
    async def get_brokerage(self, *args, **kwargs):
        return {"data": {"charges": {"total": 10.0}}}


def _store(tmp_path):
    return JournalStore(Settings(
        upstox_api_key="k", upstox_api_secret="s",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox", mobile_api_key="m", token_encryption_key="x",
        token_store_path=tmp_path / "token", journal_database_path=tmp_path / "journal.sqlite3",
    ))


async def test_reconcile_is_idempotent_and_matches_trade(tmp_path) -> None:
    store = _store(tmp_path)
    reconciler = JournalReconciler(
        store=store, upstox=_Upstox(), token_store=_Token(), ledger_store=_LedgerStore(),
    )
    first = await reconciler.reconcile()
    second = await reconciler.reconcile()
    assert first["inserted"] == 2
    assert second["inserted"] == 0
    assert store.list_trades()[1]["total_records"] == 1


async def test_reconcile_waits_for_auth(tmp_path) -> None:
    reconciler = JournalReconciler(
        store=_store(tmp_path), upstox=_Upstox(), token_store=_Token(False),
        ledger_store=_LedgerStore(),
    )
    assert await reconciler.reconcile() == {"status": "waiting_for_auth", "fills": 0}


async def test_reconcile_skips_fills_the_order_engine_never_placed(tmp_path) -> None:
    """2026-08-18 ledger-scoping fix -- a trade placed directly in the broker's own app has no
    `order_history` row at all, so it must never be journaled (this used to insert every fill
    Upstox's `get_trades_for_day` returned, account-wide)."""
    store = _store(tmp_path)
    reconciler = JournalReconciler(
        store=store, upstox=_Upstox(), token_store=_Token(),
        ledger_store=_LedgerStore(known_order_ids=("order-1",)),
    )
    result = await reconciler.reconcile()
    assert result["inserted"] == 1
    assert store.list_trades()[1]["total_records"] == 0
    fills = store.fills_for_session("2026-07-27")
    assert [f["fill_id"] for f in fills] == ["fill-1"]


class _FlakyUpstox(_Upstox):
    """Fails every /charges/brokerage call, simulating a rate limit or timeout."""
    async def get_brokerage(self, *args, **kwargs):
        raise RuntimeError("rate limited")


async def test_reconcile_keeps_last_known_charge_when_brokerage_call_fails(tmp_path) -> None:
    store = _store(tmp_path)
    good = JournalReconciler(
        store=store, upstox=_Upstox(), token_store=_Token(), ledger_store=_LedgerStore(),
    )
    await good.reconcile()
    fills_before = {f["fill_id"]: f["computed_charges"] for f in store.fills_for_session("2026-07-27")}
    assert fills_before == {"fill-1": 10.0, "fill-2": 10.0}

    # A later pass whose brokerage calls all fail must not zero out the charges that were
    # already computed -- it should retain the last known-good value instead.
    flaky = JournalReconciler(
        store=store, upstox=_FlakyUpstox(), token_store=_Token(), ledger_store=_LedgerStore(),
    )
    await flaky.reconcile()
    fills_after = {f["fill_id"]: f["computed_charges"] for f in store.fills_for_session("2026-07-27")}
    assert fills_after == fills_before

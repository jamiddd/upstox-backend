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
    reconciler = JournalReconciler(store=store, upstox=_Upstox(), token_store=_Token())
    first = await reconciler.reconcile()
    second = await reconciler.reconcile()
    assert first["inserted"] == 2
    assert second["inserted"] == 0
    assert store.list_trades()[1]["total_records"] == 1


async def test_reconcile_waits_for_auth(tmp_path) -> None:
    reconciler = JournalReconciler(
        store=_store(tmp_path), upstox=_Upstox(), token_store=_Token(False),
    )
    assert await reconciler.reconcile() == {"status": "waiting_for_auth", "fills": 0}

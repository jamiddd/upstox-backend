from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.journal_store import JournalStore
from app.services.trade_context_service import TradeContextService, extract_order_ids

pytestmark = pytest.mark.anyio


def _store(tmp_path) -> JournalStore:
    settings = Settings(
        upstox_api_key="key",
        upstox_api_secret="secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile",
        token_encryption_key="test-key",
        token_store_path=tmp_path / "token.enc",
        journal_database_path=tmp_path / "journal.sqlite3",
    )
    return JournalStore(settings)


class _Signals:
    async def get_signals(self, *args, **kwargs):
        return {"ema9_5m": 100.0}


class _FailingSignals:
    async def get_signals(self, *args, **kwargs):
        raise RuntimeError("signals unavailable")


class _Upstox:
    async def get_ltp(self, access_token, instrument_key):
        return {"data": {instrument_key: {"last_price": 88.25}}}


def test_extract_order_ids_from_sliced_gtt_response() -> None:
    payload = {
        "slices": [
            {"upstox_response": {"data": {"gtt_order_id": "gtt-1"}}},
            {"upstox_response": {"data": {"gtt_order_id": "gtt-2"}}},
        ]
    }
    assert extract_order_ids(payload) == ["gtt-1", "gtt-2"]


async def test_capture_fetches_signals_and_contract_ltp(tmp_path) -> None:
    store = _store(tmp_path)
    service = TradeContextService(store=store, upstox=_Upstox(), signals=_Signals())

    await service.capture(
        access_token="token",
        order_ids=["gtt-1"],
        trigger="placement",
        instrument_key="NSE_FO|123",
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-30",
    )

    row = store.get_context("gtt-1", "placement")
    assert row is not None
    assert row["context"] == {"ema9_5m": 100.0}
    assert row["contract_ltp"] == 88.25


async def test_capture_persists_empty_context_when_signal_call_fails(tmp_path) -> None:
    store = _store(tmp_path)
    service = TradeContextService(store=store, upstox=_Upstox(), signals=_FailingSignals())

    await service.capture(
        access_token="token",
        order_ids=["gtt-1"],
        trigger="placement",
        instrument_key="NSE_FO|123",
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date=None,
        contract_ltp=50.0,
    )

    row = store.get_context("gtt-1", "placement")
    assert row is not None
    assert row["context"] == {}
    assert row["contract_ltp"] == 50.0


async def test_fill_reuses_placement_identifiers(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_context(
        order_id="gtt-1",
        trigger="placement",
        instrument_key="NSE_FO|123",
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-30",
        contract_ltp=80.0,
        context={"placement": True},
    )
    service = TradeContextService(store=store, upstox=_Upstox(), signals=_Signals())

    await service.capture_fill_from_placement(
        access_token="token",
        order_id="gtt-1",
        contract_ltp=90.0,
    )

    row = store.get_context("gtt-1", "fill")
    assert row is not None
    assert row["underlying_key"] == "NSE_INDEX|Nifty 50"
    assert row["contract_ltp"] == 90.0

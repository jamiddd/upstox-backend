from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import Settings
from app.services.journal_store import JournalStore


def _settings(tmp_path) -> Settings:
    return Settings(
        upstox_api_key="key",
        upstox_api_secret="secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile",
        token_encryption_key="test-key",
        token_store_path=tmp_path / "token.enc",
        journal_database_path=tmp_path / "journal.sqlite3",
    )


def test_records_market_context_and_ignores_duplicate_trigger(tmp_path) -> None:
    store = JournalStore(_settings(tmp_path))
    captured_at = datetime(2026, 7, 26, 4, 5, 6, tzinfo=timezone.utc)

    inserted = store.record_context(
        order_id="order-1",
        trigger="placement",
        instrument_key="NSE_FO|123",
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-30",
        contract_ltp=125.5,
        context={"atr14_5m": 42.0, "tags": ["Above EMA"]},
        captured_at=captured_at,
    )
    duplicate = store.record_context(
        order_id="order-1",
        trigger="placement",
        instrument_key="NSE_FO|123",
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date=None,
        contract_ltp=999.0,
        context={"wrong": True},
    )

    assert inserted is True
    assert duplicate is False
    assert store.get_context("order-1", "placement") == {
        "order_id": "order-1",
        "trigger": "placement",
        "instrument_key": "NSE_FO|123",
        "underlying_key": "NSE_INDEX|Nifty 50",
        "expiry_date": "2026-07-30",
        "contract_ltp": 125.5,
        "captured_at": "2026-07-26T04:05:06+00:00",
        "context": {"atr14_5m": 42.0, "tags": ["Above EMA"]},
    }


def test_context_can_be_recorded_empty_after_signal_failure(tmp_path) -> None:
    store = JournalStore(_settings(tmp_path))
    store.record_context(
        order_id="order-2",
        trigger="fill",
        instrument_key="NSE_FO|456",
        underlying_key="NSE_INDEX|Nifty Bank",
        expiry_date=None,
        contract_ltp=None,
        context=None,
    )

    row = store.get_context("order-2", "fill")
    assert row is not None
    assert row["context"] == {}
    assert row["contract_ltp"] is None


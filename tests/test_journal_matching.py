from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.journal_store import DuplicateJournalTradeError, JournalStore


def _store(tmp_path) -> JournalStore:
    return JournalStore(Settings(
        upstox_api_key="key", upstox_api_secret="secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox", mobile_api_key="mobile",
        token_encryption_key="key", token_store_path=tmp_path / "token",
        journal_database_path=tmp_path / "journal.sqlite3",
    ))


def _fill(fill_id, side, qty, price, at):
    return {
        "fill_id": fill_id, "order_id": f"order-{fill_id}",
        "instrument_key": "NSE_FO|1", "trading_symbol": "NIFTYCE",
        "transaction_type": side, "quantity": qty, "price": price,
        "executed_at": at, "trade_date": "2026-07-27",
        "computed_charges": 10.0, "raw_payload": {},
    }


def test_fifo_matcher_keeps_multiple_same_contract_scalps_separate(tmp_path) -> None:
    store = _store(tmp_path)
    for fill in [
        _fill("1", "BUY", 75, 100, "2026-07-27T04:00:00+00:00"),
        _fill("2", "SELL", 75, 110, "2026-07-27T04:01:00+00:00"),
        _fill("3", "BUY", 75, 120, "2026-07-27T04:02:00+00:00"),
        _fill("4", "SELL", 75, 115, "2026-07-27T04:03:00+00:00"),
    ]:
        store.upsert_fill(fill)

    assert store.rebuild_session("2026-07-27") == []
    trades, page = store.list_trades()
    assert page["total_records"] == 2
    assert sorted(round(trade["gross_pnl"]) for trade in trades) == [-375, 750]
    assert all(trade["computed_charges"] == 20 for trade in trades)


def test_same_fill_set_preserves_identity_and_notes(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert_fill(_fill("1", "BUY", 75, 100, "2026-07-27T04:00:00+00:00"))
    store.upsert_fill(_fill("2", "SELL", 75, 110, "2026-07-27T04:01:00+00:00"))
    store.rebuild_session("2026-07-27")
    trade = store.list_trades()[0][0]
    store.save_notes(trade["id"], {"notes": "Good entry", "tags": ["breakout"], "reviewed": True})

    store.rebuild_session("2026-07-27")
    rebuilt = store.get_trade(trade["id"])
    assert rebuilt is not None
    assert rebuilt["notes"] == "Good entry"
    assert rebuilt["tags"] == ["breakout"]


def test_manual_trade_and_analytics(tmp_path) -> None:
    store = _store(tmp_path)
    created = store.create_manual_trade({
        "instrument_key": "manual", "trading_symbol": "NIFTYCE",
        "trade_date": "2026-07-27", "direction": "long", "quantity": 75,
        "entry_price": 100, "exit_price": 110,
        "opened_at": "2026-07-27T04:00:00+00:00",
        "closed_at": "2026-07-27T04:01:00+00:00",
        "gross_pnl": 750, "charges": 20,
        "journal": {"setup": "breakout", "tags": ["A+"], "reviewed": True},
    })
    assert created["net_pnl"] == 730
    summary = store.analytics_summary()
    assert summary["trade_count"] == 1
    assert summary["net_pnl"] == 730
    assert summary["low_sample"] is True


def test_manual_trade_rejects_existing_automatic_trade(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert_fill(_fill("1", "BUY", 75, 100, "2026-07-27T04:00:25+00:00"))
    store.upsert_fill(_fill("2", "SELL", 75, 110, "2026-07-27T04:01:20+00:00"))
    store.rebuild_session("2026-07-27")
    existing = store.list_trades()[0][0]

    with pytest.raises(DuplicateJournalTradeError) as error:
        store.create_manual_trade({
            "instrument_key": "manual", "trading_symbol": "nifty ce",
            "trade_date": "2026-07-27", "direction": "long", "quantity": 75,
            "entry_price": 100.05, "exit_price": 109.95,
            "opened_at": "2026-07-27T04:00:00+00:00",
            "closed_at": "2026-07-27T04:01:00+00:00",
            "gross_pnl": 742.5, "charges": 20,
        })

    assert error.value.trade_id == existing["id"]
    assert store.list_trades()[1]["total_records"] == 1


def test_late_automatic_trade_upgrades_manual_entry_and_preserves_notes(tmp_path) -> None:
    store = _store(tmp_path)
    manual = store.create_manual_trade({
        "instrument_key": "manual", "trading_symbol": "NIFTY CE",
        "trade_date": "2026-07-27", "direction": "long", "quantity": 75,
        "entry_price": 100.05, "exit_price": 109.95,
        "opened_at": "2026-07-27T04:00:00+00:00",
        "closed_at": "2026-07-27T04:01:00+00:00",
        "gross_pnl": 742.5, "charges": 25,
        "journal": {"notes": "Entered before sync", "tags": ["breakout"]},
    })

    store.upsert_fill(_fill("1", "BUY", 75, 100, "2026-07-27T04:00:25+00:00"))
    store.upsert_fill(_fill("2", "SELL", 75, 110, "2026-07-27T04:01:20+00:00"))
    store.rebuild_session("2026-07-27")

    trades, page = store.list_trades()
    assert page["total_records"] == 1
    upgraded = store.get_trade(manual["id"])
    assert upgraded is not None
    assert upgraded["source"] == "automatic"
    assert upgraded["instrument_key"] == "NSE_FO|1"
    assert upgraded["gross_pnl"] == 750
    assert upgraded["computed_charges"] == 20
    assert upgraded["manual_charge_override"] is None
    assert upgraded["notes"] == "Entered before sync"
    assert upgraded["tags"] == ["breakout"]
    assert trades[0]["id"] == manual["id"]

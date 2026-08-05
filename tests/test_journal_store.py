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


def test_latest_placement_context_returns_most_recent_by_instrument(tmp_path) -> None:
    store = JournalStore(_settings(tmp_path))
    store.record_context(
        order_id="order-a",
        trigger="placement",
        instrument_key="NSE_FO|789",
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-30",
        contract_ltp=100.0,
        context={"tags": ["first"]},
        captured_at=datetime(2026, 7, 26, 4, 0, 0, tzinfo=timezone.utc),
    )
    store.record_context(
        order_id="order-b",
        trigger="placement",
        instrument_key="NSE_FO|789",
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-30",
        contract_ltp=110.0,
        context={"tags": ["second"]},
        captured_at=datetime(2026, 7, 26, 4, 30, 0, tzinfo=timezone.utc),
    )

    latest = store.latest_placement_context("NSE_FO|789")

    assert latest is not None
    assert latest["order_id"] == "order-b"
    assert latest["contract_ltp"] == 110.0
    assert latest["context"] == {"tags": ["second"]}
    assert store.latest_placement_context("NSE_FO|unknown") is None


def _fill(fill_id: str, trade_date: str, computed_charges: float) -> dict:
    return {
        "fill_id": fill_id,
        "order_id": f"order-{fill_id}",
        "instrument_key": "NSE_FO|123",
        "trading_symbol": "NIFTY26JUL25000CE",
        "transaction_type": "BUY",
        "quantity": 75,
        "price": 100.0,
        "executed_at": f"{trade_date}T04:00:00+00:00",
        "trade_date": trade_date,
        "exchange": "NFO",
        "segment": "FO",
        "option_type": "CE",
        "strike_price": 25000.0,
        "expiry": "2026-07-30",
        "computed_charges": computed_charges,
    }


def test_total_charges_for_date_sums_only_that_dates_fills(tmp_path) -> None:
    store = JournalStore(_settings(tmp_path))
    store.upsert_fill(_fill("fill-1", "2026-07-26", 12.5))
    store.upsert_fill(_fill("fill-2", "2026-07-26", 7.25))
    store.upsert_fill(_fill("fill-3", "2026-07-25", 999.0))  # a different day -- must not count

    assert store.total_charges_for_date("2026-07-26") == 19.75
    assert store.total_charges_for_date("2026-07-25") == 999.0
    assert store.total_charges_for_date("2026-07-27") == 0.0


def _closed_round_trip(
    store: JournalStore, trade_date: str, buy_id: str, sell_id: str, *, charges_per_fill: float = 5.0,
) -> None:
    buy = _fill(buy_id, trade_date, charges_per_fill)
    buy["transaction_type"] = "BUY"
    sell = dict(buy)
    sell.update(fill_id=sell_id, order_id=f"order-{sell_id}", transaction_type="SELL",
                price=110.0, executed_at=f"{trade_date}T04:05:00+00:00", computed_charges=charges_per_fill)
    store.upsert_fill(buy)
    store.upsert_fill(sell)
    store.rebuild_session(trade_date)


def test_weekday_breakdown_always_returns_seven_days_monday_first(tmp_path) -> None:
    store = JournalStore(_settings(tmp_path))
    # 2026-07-27 is a Monday, 2026-07-28 a Tuesday -- no trades on any other day of that week.
    _closed_round_trip(store, "2026-07-27", "mon-buy", "mon-sell")
    _closed_round_trip(store, "2026-07-28", "tue-buy", "tue-sell")

    breakdown = store.analytics_summary()["weekday_breakdown"]

    assert [day["label"] for day in breakdown] == [
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    ]
    by_label = {day["label"]: day for day in breakdown}
    assert by_label["Monday"]["trade_count"] == 1
    assert by_label["Monday"]["net_pnl"] == 740.0  # (110-100)*75 - (5+5) charges
    assert by_label["Tuesday"]["trade_count"] == 1
    # Days with no trades in range still show up, zeroed out rather than being omitted.
    for empty_label in ("Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        assert by_label[empty_label] == {
            "label": empty_label, "trade_count": 0, "net_pnl": 0.0,
            "win_rate": 0.0, "low_sample": True,
        }


def test_backfill_flat_brokerage_correction_fixes_fills_and_rebuilds_journal_trades(tmp_path) -> None:
    store = JournalStore(_settings(tmp_path))
    # Each fill's stored charge still has the old, overstated Upstox flat-brokerage figure
    # baked in (30 instead of the real 20 -- see UpstoxService.get_brokerage's own doc comment).
    _closed_round_trip(store, "2026-07-27", "buy-1", "sell-1", charges_per_fill=32.5)
    # A fallback 0.0 from a failed charge lookup (see JournalReconciler._charges) -- below the
    # correction threshold, must be left alone rather than pushed negative.
    store.upsert_fill(_fill("fallback-fill", "2026-07-28", 0.0))

    result = store.backfill_flat_brokerage_correction()

    assert result == {"already_ran": False, "fills_corrected": 2, "trading_dates_rebuilt": 2}
    fills = {f["fill_id"]: f["computed_charges"] for f in store.fills_for_session("2026-07-27")}
    assert fills == {"buy-1": 22.5, "sell-1": 22.5}
    untouched = store.fills_for_session("2026-07-28")
    assert untouched[0]["computed_charges"] == 0.0

    # journal_trades.computed_charges (derived from the fills via rebuild_session) reflects the
    # correction too, not just the raw fill rows.
    trade = store.list_trades()[0][0]
    assert trade["computed_charges"] == 45.0  # 22.5 + 22.5

    # Idempotent: a second call is a no-op, doesn't double-correct.
    again = store.backfill_flat_brokerage_correction()
    assert again == {"already_ran": True, "fills_corrected": 0, "trading_dates_rebuilt": 0}
    fills_after = {f["fill_id"]: f["computed_charges"] for f in store.fills_for_session("2026-07-27")}
    assert fills_after == {"buy-1": 22.5, "sell-1": 22.5}


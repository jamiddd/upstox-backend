from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.services.signal_snapshot_store import SignalSnapshotStore

_IST = ZoneInfo("Asia/Kolkata")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="",
        upstox_api_secret="",
        upstox_redirect_url="",
        upstox_environment="sandbox",
        mobile_api_key="",
        token_encryption_key="",
        token_store_path=tmp_path / "token.enc",
        oi_database_path=tmp_path / "market.sqlite3",
    )


def _metrics(atr: float, *, support_strike: float = 25000) -> dict:
    return {
        "atr": atr,
        "vwap_distance": 12.0,
        "level_distance": 8.0,
        "pcr": 1.2,
        "support_strike": support_strike,
        "support_oi": 1_000_000,
        "support_call_oi": 500_000,
        "resistance_strike": 25200,
        "resistance_oi": 1_100_000,
        "resistance_put_oi": 450_000,
        "atm_straddle": 240,
    }


def _record(store: SignalSnapshotStore, observed_at: datetime, atr: float):
    return store.record_and_find_previous(
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-23",
        observed_at=observed_at,
        metrics=_metrics(atr),
        minimum_age_seconds=240,
        maximum_age_seconds=360,
        target_age_seconds=300,
    )


def test_history_survives_store_recreation_and_finds_five_minute_snapshot(tmp_path: Path) -> None:
    first = datetime(2026, 7, 21, 9, 15, 10, tzinfo=_IST)
    store = SignalSnapshotStore(_settings(tmp_path))
    assert _record(store, first, 20.0) is None

    # A new instance simulates a process restart; the earlier row is still used for the delta.
    recreated = SignalSnapshotStore(_settings(tmp_path))
    previous = _record(recreated, first + timedelta(minutes=5), 23.5)

    assert previous is not None
    assert previous["atr"] == 20.0
    assert previous["support_strike"] == 25000.0


def test_one_snapshot_is_kept_per_five_minute_slot(tmp_path: Path) -> None:
    store = SignalSnapshotStore(_settings(tmp_path))
    first = datetime(2026, 7, 21, 10, 1, tzinfo=_IST)
    _record(store, first, 20.0)
    _record(store, first + timedelta(minutes=1), 99.0)

    snapshots = store.list_snapshots(
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-23",
    )
    assert len(snapshots) == 1
    assert snapshots[0]["atr"] == 20.0
    assert snapshots[0]["slot_start"] == "2026-07-21T04:30:00+00:00"


def test_history_listing_is_newest_first_and_can_span_expiries(tmp_path: Path) -> None:
    store = SignalSnapshotStore(_settings(tmp_path))
    first = datetime(2026, 7, 21, 9, 15, tzinfo=_IST)
    _record(store, first, 20.0)
    store.record_and_find_previous(
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-30",
        observed_at=first + timedelta(days=1),
        metrics=_metrics(30.0),
        minimum_age_seconds=240,
        maximum_age_seconds=360,
        target_age_seconds=300,
    )

    snapshots = store.list_snapshots(underlying_key="NSE_INDEX|Nifty 50")
    assert [row["expiry_date"] for row in snapshots] == ["2026-07-30", "2026-07-23"]
    assert [row["atr"] for row in snapshots] == [30.0, 20.0]


def test_overnight_cleanup_keeps_current_and_future_expiries(tmp_path: Path) -> None:
    store = SignalSnapshotStore(_settings(tmp_path))
    observed = datetime(2026, 7, 23, 15, 25, tzinfo=_IST)
    _record(store, observed, 20.0)
    store.record_and_find_previous(
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-30",
        observed_at=observed,
        metrics=_metrics(30.0),
        minimum_age_seconds=240,
        maximum_age_seconds=360,
        target_age_seconds=300,
    )

    assert store.delete_expired_before(date(2026, 7, 24)) == 1
    snapshots = store.list_snapshots(underlying_key="NSE_INDEX|Nifty 50")
    assert [row["expiry_date"] for row in snapshots] == ["2026-07-30"]

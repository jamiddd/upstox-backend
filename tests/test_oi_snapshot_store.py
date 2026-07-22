from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.services.oi_snapshot_store import (
    OISnapshotStore,
    OiStrikeDiff,
    OiStrikesDiff,
    SnapshotNotFoundError,
)

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
        oi_database_path=tmp_path / "oi.sqlite3",
    )


def _analysis(expiry: str = "2026-07-23") -> dict:
    return {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "expiry": expiry,
        "oi": {
            "total_calls": 1000,
            "total_puts": 1250,
            "call_put_oi_data_list": [
                {"strike_price": 25000, "call_oi": 600, "put_oi": 700},
                {"strike_price": 25100, "call_oi": 400, "put_oi": 550},
            ],
        },
        "change_oi": {
            "call_put_oi_data_list": [
                {"strike_price": 25000, "call_change_oi": -20, "put_change_oi": 35},
            ],
        },
        "max_pain": {"max_pain": 25000, "insights": []},
        "pcr": {"pcr": 1.25, "insights": []},
    }


def test_saves_lossless_snapshot_and_normalized_strikes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = OISnapshotStore(settings)
    slot = datetime(2026, 7, 23, 9, 15, tzinfo=_IST)

    assert store.save_snapshot(
        underlying_key="NSE_INDEX|Nifty 50",
        underlying_symbol="NIFTY",
        expiry_date="2026-07-23",
        slot_start=slot,
        observed_at=slot,
        analysis=_analysis(),
    ) is True
    assert store.has_snapshot("NSE_INDEX|Nifty 50", "2026-07-23", slot) is True

    with sqlite3.connect(settings.oi_database_path) as connection:
        snapshot = connection.execute(
            "SELECT total_call_oi, total_put_oi, pcr, max_pain, payload_json FROM oi_snapshots",
        ).fetchone()
        strikes = connection.execute(
            """
            SELECT strike_price, call_oi, put_oi, call_change_oi, put_change_oi
            FROM oi_strikes ORDER BY strike_price
            """,
        ).fetchall()

    assert snapshot[:4] == (1000.0, 1250.0, 1.25, 25000.0)
    assert json.loads(snapshot[4]) == _analysis()
    assert strikes == [
        (25000.0, 600.0, 700.0, -20.0, 35.0),
        (25100.0, 400.0, 550.0, None, None),
    ]


def test_duplicate_slot_is_idempotent(tmp_path: Path) -> None:
    store = OISnapshotStore(_settings(tmp_path))
    slot = datetime(2026, 7, 23, 10, 0, tzinfo=_IST)
    kwargs = {
        "underlying_key": "NSE_INDEX|Nifty 50",
        "underlying_symbol": "NIFTY",
        "expiry_date": "2026-07-23",
        "slot_start": slot,
        "observed_at": slot,
        "analysis": _analysis(),
    }

    assert store.save_snapshot(**kwargs) is True
    assert store.save_snapshot(**kwargs) is False


def test_overnight_cleanup_deletes_only_earlier_expiries_and_cascades(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = OISnapshotStore(settings)
    for day in (23, 30):
        slot = datetime(2026, 7, day, 15, 25, tzinfo=_IST)
        store.save_snapshot(
            underlying_key="NSE_INDEX|Nifty 50",
            underlying_symbol="NIFTY",
            expiry_date=f"2026-07-{day}",
            slot_start=slot,
            observed_at=slot,
            analysis=_analysis(f"2026-07-{day}"),
        )

    assert store.delete_expired_before(date(2026, 7, 24)) == 1
    with sqlite3.connect(settings.oi_database_path) as connection:
        expiries = connection.execute("SELECT expiry_date FROM oi_snapshots").fetchall()
        strike_count = connection.execute("SELECT COUNT(*) FROM oi_strikes").fetchone()[0]

    assert expiries == [("2026-07-30",)]
    assert strike_count == 2


def test_lists_lightweight_snapshot_summaries_newest_first(tmp_path: Path) -> None:
    store = OISnapshotStore(_settings(tmp_path))
    for day, hour in ((23, 9), (30, 10)):
        slot = datetime(2026, 7, day, hour, 15, tzinfo=_IST)
        store.save_snapshot(
            underlying_key="NSE_INDEX|Nifty 50",
            underlying_symbol="NIFTY",
            expiry_date=f"2026-07-{day}",
            slot_start=slot,
            observed_at=slot,
            analysis=_analysis(f"2026-07-{day}"),
        )

    across_expiries = store.list_snapshots(
        underlying_key="NSE_INDEX|Nifty 50",
        limit=1,
    )
    assert across_expiries == [
        {
            "expiry_date": "2026-07-30",
            "trading_date": "2026-07-30",
            "slot_start": "2026-07-30T04:45:00+00:00",
            "observed_at": "2026-07-30T04:45:00+00:00",
            "total_call_oi": 1000.0,
            "total_put_oi": 1250.0,
            "pcr": 1.25,
            "max_pain": 25000.0,
        },
    ]
    filtered = store.list_snapshots(
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-23",
    )
    assert len(filtered) == 1
    assert filtered[0]["expiry_date"] == "2026-07-23"


def test_diffs_totals_and_matching_strikes_between_exact_slots(tmp_path: Path) -> None:
    store = OISnapshotStore(_settings(tmp_path))
    before_slot = datetime(2026, 7, 23, 9, 30, tzinfo=_IST)
    after_slot = datetime(2026, 7, 23, 10, 15, tzinfo=_IST)
    before = _analysis()
    after = _analysis()
    after["oi"] = {
        "total_calls": 1500,
        "total_puts": 1100,
        "call_put_oi_data_list": [
            {"strike_price": 25000, "call_oi": 750, "put_oi": 650},
            {"strike_price": 25100, "call_oi": 450, "put_oi": 700},
            # A strike appearing in only one snapshot cannot be compared and is omitted.
            {"strike_price": 25200, "call_oi": 300, "put_oi": 100},
        ],
    }
    for slot, analysis in ((before_slot, before), (after_slot, after)):
        store.save_snapshot(
            underlying_key="NSE_INDEX|Nifty 50",
            underlying_symbol="NIFTY",
            expiry_date="2026-07-23",
            slot_start=slot,
            observed_at=slot,
            analysis=analysis,
        )

    result = store.diff_strikes(
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-23",
        from_slot=before_slot,
        to_slot=after_slot,
    )

    assert result == OiStrikesDiff(
        underlying_symbol="NIFTY",
        total_call_oi_change=500.0,
        total_put_oi_change=-150.0,
        strikes=[
            # (strike_price, call_oi_change, put_oi_change, call_oi, put_oi) -- the last two are
            # each strike's *absolute* OI as of the later (to_slot) snapshot, not diffed.
            OiStrikeDiff(25000.0, 150.0, -50.0, 750.0, 650.0),
            OiStrikeDiff(25100.0, 50.0, 150.0, 450.0, 700.0),
            # Missing from the earlier snapshot means a zero baseline, not an omitted strike.
            OiStrikeDiff(25200.0, 300.0, 100.0, 300.0, 100.0),
        ],
    )
    missing_slot = after_slot.replace(minute=20)
    with pytest.raises(SnapshotNotFoundError) as captured:
        store.diff_strikes(
            underlying_key="NSE_INDEX|Nifty 50",
            expiry_date="2026-07-23",
            from_slot=before_slot,
            to_slot=missing_slot,
        )
    assert captured.value.which == "to_slot"
    assert captured.value.slot == missing_slot


def test_find_snapshot_strikes_in_band_returns_the_closest_in_band_snapshot(tmp_path: Path) -> None:
    store = OISnapshotStore(_settings(tmp_path))
    now = datetime(2026, 7, 23, 9, 45, tzinfo=_IST)
    # Too recent (2 minutes) -- outside the 4-6 minute band, must not be picked.
    store.save_snapshot(
        underlying_key="NSE_INDEX|Nifty 50", underlying_symbol="NIFTY", expiry_date="2026-07-23",
        slot_start=now.replace(minute=43), observed_at=now.replace(minute=43), analysis=_analysis(),
    )
    # 5 minutes old -- squarely in-band, this is the one that should be returned.
    in_band_analysis = _analysis()
    in_band_analysis["oi"]["call_put_oi_data_list"] = [
        {"strike_price": 25000, "call_oi": 500, "put_oi": 900},
        {"strike_price": 25100, "call_oi": 400, "put_oi": 550},
    ]
    store.save_snapshot(
        underlying_key="NSE_INDEX|Nifty 50", underlying_symbol="NIFTY", expiry_date="2026-07-23",
        slot_start=now.replace(minute=40), observed_at=now.replace(minute=40), analysis=in_band_analysis,
    )
    # 9 minutes old -- outside the band on the far side, must not be picked either.
    store.save_snapshot(
        underlying_key="NSE_INDEX|Nifty 50", underlying_symbol="NIFTY", expiry_date="2026-07-23",
        slot_start=now.replace(minute=36), observed_at=now.replace(minute=36), analysis=_analysis(),
    )

    strikes = store.find_snapshot_strikes_in_band(
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-23",
        now=now,
        minimum_age_seconds=240.0,
        maximum_age_seconds=360.0,
        target_age_seconds=300.0,
    )

    assert strikes == {25000.0: (500.0, 900.0), 25100.0: (400.0, 550.0)}


def test_find_snapshot_strikes_in_band_is_none_with_nothing_in_band(tmp_path: Path) -> None:
    store = OISnapshotStore(_settings(tmp_path))
    now = datetime(2026, 7, 23, 9, 45, tzinfo=_IST)
    store.save_snapshot(
        underlying_key="NSE_INDEX|Nifty 50", underlying_symbol="NIFTY", expiry_date="2026-07-23",
        slot_start=now.replace(minute=43), observed_at=now.replace(minute=43), analysis=_analysis(),
    )

    strikes = store.find_snapshot_strikes_in_band(
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-23",
        now=now,
        minimum_age_seconds=240.0,
        maximum_age_seconds=360.0,
        target_age_seconds=300.0,
    )

    assert strikes is None


def test_find_snapshot_strikes_in_band_strike_not_in_the_matched_snapshot_is_absent(tmp_path: Path) -> None:
    store = OISnapshotStore(_settings(tmp_path))
    now = datetime(2026, 7, 23, 9, 45, tzinfo=_IST)
    store.save_snapshot(
        underlying_key="NSE_INDEX|Nifty 50", underlying_symbol="NIFTY", expiry_date="2026-07-23",
        slot_start=now.replace(minute=40), observed_at=now.replace(minute=40), analysis=_analysis(),
    )

    strikes = store.find_snapshot_strikes_in_band(
        underlying_key="NSE_INDEX|Nifty 50",
        expiry_date="2026-07-23",
        now=now,
        minimum_age_seconds=240.0,
        maximum_age_seconds=360.0,
        target_age_seconds=300.0,
    )

    # 25200 was never in the stored chain at all -- a caller looking that strike up gets a plain
    # dict miss (None per side), same as any other strike not present in the window.
    assert 25200.0 not in strikes

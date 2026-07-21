from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.services.oi_snapshot_store import OISnapshotStore

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

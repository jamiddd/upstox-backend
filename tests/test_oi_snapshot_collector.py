from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import anyio

from app.services import oi_snapshot_collector as collector

_IST = ZoneInfo("Asia/Kolkata")


class _TokenStore:
    def has_token(self) -> bool:
        return True

    def load_access_token(self) -> str:
        return "token"


class _TrackedStore:
    def load(self) -> list[str]:
        return ["NSE_INDEX|Nifty 50"]


class _MainScreen:
    calls = 0

    async def resolve_underlying_symbol_and_expiry(
        self, access_token: str, underlying_key: str,
    ) -> tuple[str, Optional[str]]:
        self.calls += 1
        return "NIFTY", "2026-07-23"


class _AnalysisService:
    calls: list[dict[str, Any]]

    def __init__(self) -> None:
        self.calls = []

    async def get_analysis(self, access_token: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "expiry": "2026-07-23",
            "oi": {"call_put_oi_data_list": []},
            "change_oi": {"call_put_oi_data_list": []},
            "max_pain": {},
            "pcr": {},
        }


class _SnapshotStore:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.cleanup: list[date] = []
        self.saved: list[dict[str, Any]] = []

    def delete_expired_before(self, cutoff: date) -> int:
        self.cleanup.append(cutoff)
        return 0

    def has_snapshot(self, underlying_key: str, expiry_date: str, slot_start: datetime) -> bool:
        return self.exists

    def save_snapshot(self, **kwargs: Any) -> bool:
        self.saved.append(kwargs)
        return True


def test_market_slot_is_aligned_and_excludes_close() -> None:
    assert collector._market_slot(datetime(2026, 7, 21, 9, 17, 42, tzinfo=_IST)) == datetime(
        2026, 7, 21, 9, 15, tzinfo=_IST,
    )
    assert collector._market_slot(datetime(2026, 7, 21, 15, 29, tzinfo=_IST)) == datetime(
        2026, 7, 21, 15, 25, tzinfo=_IST,
    )
    assert collector._market_slot(datetime(2026, 7, 21, 15, 30, tzinfo=_IST)) is None


def test_collects_one_snapshot_for_current_slot_and_runs_daily_cleanup() -> None:
    store = _SnapshotStore()
    analysis = _AnalysisService()
    now = datetime(2026, 7, 21, 9, 17, 42, tzinfo=_IST)

    completed = anyio.run(
        lambda: collector._collect_tick(
            now=now,
            token_store=_TokenStore(),
            tracked_store=_TrackedStore(),
            main_screen=_MainScreen(),
            analysis_service=analysis,
            snapshot_store=store,
            cleanup_completed_for=None,
        ),
    )

    assert completed == date(2026, 7, 21)
    assert store.cleanup == [date(2026, 7, 21)]
    assert analysis.calls[0]["bucket_interval"] == 5
    assert analysis.calls[0]["date"] == "2026-07-21"
    assert store.saved[0]["slot_start"] == datetime(2026, 7, 21, 9, 15, tzinfo=_IST)


def test_closed_market_still_performs_overnight_cleanup() -> None:
    store = _SnapshotStore()
    analysis = _AnalysisService()

    anyio.run(
        lambda: collector._collect_tick(
            now=datetime(2026, 7, 24, 0, 0, 5, tzinfo=_IST),
            token_store=_TokenStore(),
            tracked_store=_TrackedStore(),
            main_screen=_MainScreen(),
            analysis_service=analysis,
            snapshot_store=store,
            cleanup_completed_for=date(2026, 7, 23),
        ),
    )

    assert store.cleanup == [date(2026, 7, 24)]
    assert analysis.calls == []
    assert store.saved == []


def test_existing_slot_skips_upstream_analytics_call() -> None:
    store = _SnapshotStore(exists=True)
    analysis = _AnalysisService()

    anyio.run(
        lambda: collector._collect_tick(
            now=datetime(2026, 7, 21, 10, 0, tzinfo=_IST),
            token_store=_TokenStore(),
            tracked_store=_TrackedStore(),
            main_screen=_MainScreen(),
            analysis_service=analysis,
            snapshot_store=store,
            cleanup_completed_for=date(2026, 7, 21),
        ),
    )

    assert analysis.calls == []
    assert store.saved == []

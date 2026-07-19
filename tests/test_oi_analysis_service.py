from __future__ import annotations

import asyncio

import anyio
import pytest

from app.core.exceptions import UpstoxApiError
from app.services import oi_analysis_service
from app.services.oi_analysis_service import OIAnalysisService


@pytest.fixture(autouse=True)
def clear_oi_analysis_cache() -> None:
    oi_analysis_service._CACHE = {}


class _FakeUpstoxService:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.active_calls = 0
        self.max_active_calls = 0

    async def _response(self, name: str, data: dict) -> dict:
        self.calls.append(name)
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        await asyncio.sleep(0)
        self.active_calls -= 1
        return {"status": "success", "data": data}

    async def get_oi(self, access_token, instrument_key, *, expiry, date):
        return await self._response(
            "oi",
            {
                "expiry": "2026-07-23",
                "total_puts": 12500000,
                "total_calls": 9800000,
                "call_put_oi_data_list": [],
            },
        )

    async def get_change_oi(self, access_token, instrument_key, *, expiry, date, interval):
        return await self._response("change_oi", {"total_put_change_oi": 2500000})

    async def get_max_pain(self, access_token, instrument_key, *, expiry, date, bucket_interval):
        return await self._response("max_pain", {"max_pain": 25000.0, "insights": []})

    async def get_pcr(self, access_token, instrument_key, *, expiry, date, bucket_interval):
        return await self._response("pcr", {"pcr": 1.2755, "insights": []})


def test_get_analysis_combines_four_concurrent_calls_and_resolves_expiry() -> None:
    upstox = _FakeUpstoxService()
    service = OIAnalysisService(upstox)

    result = anyio.run(
        lambda: service.get_analysis(
            "upstox-token",
            instrument_key="NSE_INDEX|Nifty 50",
            expiry="current_week",
            date="2026-07-17",
            change_interval=2,
            bucket_interval=30,
        )
    )

    assert set(upstox.calls) == {"oi", "change_oi", "max_pain", "pcr"}
    assert upstox.max_active_calls == 4
    assert result == {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "expiry": "2026-07-23",
        "date": "2026-07-17",
        "change_interval": 2,
        "bucket_interval": 30,
        "oi": {
            "expiry": "2026-07-23",
            "total_puts": 12500000,
            "total_calls": 9800000,
            "call_put_oi_data_list": [],
        },
        "change_oi": {"total_put_change_oi": 2500000},
        "max_pain": {"max_pain": 25000.0, "insights": []},
        "pcr": {"pcr": 1.2755, "insights": []},
    }


def test_get_analysis_caches_identical_parameter_sets() -> None:
    upstox = _FakeUpstoxService()
    service = OIAnalysisService(upstox)

    async def run() -> None:
        kwargs = {
            "instrument_key": "NSE_INDEX|Nifty 50",
            "expiry": "current_week",
            "date": "2026-07-17",
            "change_interval": 1,
            "bucket_interval": 60,
        }
        first = await service.get_analysis("upstox-token", **kwargs)
        second = await service.get_analysis("upstox-token", **kwargs)
        assert second is first

    anyio.run(run)
    assert len(upstox.calls) == 4


def test_get_analysis_rejects_a_malformed_upstream_section() -> None:
    class _MalformedPcrService(_FakeUpstoxService):
        async def get_pcr(self, access_token, instrument_key, *, expiry, date, bucket_interval):
            return {"status": "success", "data": []}

    service = OIAnalysisService(_MalformedPcrService())

    async def run() -> None:
        await service.get_analysis(
            "upstox-token",
            instrument_key="NSE_INDEX|Nifty 50",
            expiry="current_week",
            date="2026-07-17",
            change_interval=1,
            bucket_interval=60,
        )

    with pytest.raises(UpstoxApiError, match="/market/pcr"):
        anyio.run(run)

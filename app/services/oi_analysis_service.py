from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any, Optional

from app.core.exceptions import UpstoxApiError
from app.services.upstox_service import UpstoxService


@dataclass
class _CacheEntry:
    expires_at: float
    value: dict[str, Any]


_CACHE: dict[tuple[Any, ...], _CacheEntry] = {}


class OIAnalysisService:
    """Combine Upstox's four complementary OI analytics APIs into one client response.

    PCR's headline value and current max pain overlap with the raw OI series, but their APIs
    also include intraday insight history. Change OI likewise needs a comparison snapshot. We
    therefore preserve Upstox as the source of truth and call all four APIs concurrently.
    """

    def __init__(self, upstox_service: UpstoxService) -> None:
        self.upstox = upstox_service

    async def get_analysis(
        self,
        access_token: str,
        *,
        instrument_key: str,
        expiry: str,
        date: str,
        change_interval: int,
        bucket_interval: int,
    ) -> dict[str, Any]:
        cache_key = (
            instrument_key,
            expiry,
            date,
            change_interval,
            bucket_interval,
        )
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        oi_payload, change_payload, max_pain_payload, pcr_payload = await asyncio.gather(
            self.upstox.get_oi(
                access_token,
                instrument_key,
                expiry=expiry,
                date=date,
            ),
            self.upstox.get_change_oi(
                access_token,
                instrument_key,
                expiry=expiry,
                date=date,
                interval=change_interval,
            ),
            self.upstox.get_max_pain(
                access_token,
                instrument_key,
                expiry=expiry,
                date=date,
                bucket_interval=bucket_interval,
            ),
            self.upstox.get_pcr(
                access_token,
                instrument_key,
                expiry=expiry,
                date=date,
                bucket_interval=bucket_interval,
            ),
        )

        oi = _data_object(oi_payload, endpoint="/market/oi")
        change_oi = _data_object(change_payload, endpoint="/market/change-oi")
        max_pain = _data_object(max_pain_payload, endpoint="/market/max-pain")
        pcr = _data_object(pcr_payload, endpoint="/market/pcr")

        resolved_expiry = oi.get("expiry")
        if not isinstance(resolved_expiry, str) or not resolved_expiry:
            resolved_expiry = expiry

        result = {
            "instrument_key": instrument_key,
            "expiry": resolved_expiry,
            "date": date,
            "change_interval": change_interval,
            "bucket_interval": bucket_interval,
            "oi": oi,
            "change_oi": change_oi,
            "max_pain": max_pain,
            "pcr": pcr,
        }
        _cache_set(cache_key, result, ttl_seconds=60.0)
        return result


def _data_object(payload: dict[str, Any], *, endpoint: str) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise UpstoxApiError(f"Unexpected Upstox response from {endpoint}")
    return data


def _cache_get(key: tuple[Any, ...]) -> Optional[dict[str, Any]]:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    if entry.expires_at <= monotonic():
        _CACHE.pop(key, None)
        return None
    return entry.value


def _cache_set(key: tuple[Any, ...], value: dict[str, Any], *, ttl_seconds: float) -> None:
    _CACHE[key] = _CacheEntry(expires_at=monotonic() + ttl_seconds, value=value)

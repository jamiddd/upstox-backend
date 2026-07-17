from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, Optional

from app.services.upstox_service import UpstoxService


@dataclass
class _SearchCacheEntry:
    expires_at: float
    value: dict[str, Any]


_SEARCH_CACHE: dict[tuple[str, int], _SearchCacheEntry] = {}

DEFAULT_OPTION_INDICES = [
    {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "symbol": "NIFTY",
        "name": "Nifty 50",
        "underlying_type": "INDEX",
        "exchange": "NSE",
        "lot_size": 65.0,
        "freeze_quantity": 1755.0,
        "tick_size": 0.05,
    },
    {
        "instrument_key": "NSE_INDEX|Nifty Bank",
        "symbol": "BANKNIFTY",
        "name": "Nifty Bank",
        "underlying_type": "INDEX",
        "exchange": "NSE",
        "lot_size": 30.0,
        "freeze_quantity": 600.0,
        "tick_size": 0.05,
    },
    {
        "instrument_key": "NSE_INDEX|Nifty Fin Service",
        "symbol": "FINNIFTY",
        "name": "Nifty Fin Service",
        "underlying_type": "INDEX",
        "exchange": "NSE",
        "lot_size": 65.0,
        "freeze_quantity": 1755.0,
        "tick_size": 0.05,
    },
    {
        # FIX: this was "NSE_INDEX|Nifty Midcap Select" -- not a real Upstox instrument key (the
        # real one uses the exchange's actual index name, "NIFTY MID SELECT", confirmed against
        # Upstox's own instrument master). The typo'd key doesn't match anything on Upstox's side,
        # so the option-contract lookup silently returned zero contracts for it -- the backend
        # then correctly returned expiries: [] / selected_expiry: null, but the Android app's
        # bootstrap DTO used to declare selectedExpiry non-nullable, so selecting MIDCPNIFTY
        # crashed instead of just showing "no contracts" (that's now fixed on the app side too).
        "instrument_key": "NSE_INDEX|NIFTY MID SELECT",
        "symbol": "MIDCPNIFTY",
        "name": "Nifty Midcap Select",
        "underlying_type": "INDEX",
        "exchange": "NSE",
        "lot_size": 120.0,
        "freeze_quantity": 2800.0,
        "tick_size": 0.05,
    },
]


class SearchScreenService:
    """Build search-screen payloads for option-capable underlyings."""

    def __init__(self, upstox_service: UpstoxService) -> None:
        self.upstox = upstox_service

    async def search_underlyings(
        self,
        access_token: str,
        *,
        query: str,
        limit: int = 20,
        page_number: int = 1,
    ) -> dict[str, Any]:
        """Return deduped index/equity underlyings that have option contracts."""
        normalized_query = query.strip()
        safe_limit = min(max(limit, 1), 30)
        safe_page = max(page_number, 1)
        if not normalized_query:
            return _default_indices(limit=safe_limit, page_number=safe_page)

        cache_key = (normalized_query.upper(), safe_limit, safe_page)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        payload = await self.upstox.search_instruments(
            access_token,
            query=normalized_query,
            page_number=safe_page,
            records=safe_limit,
        )
        results = _shape_underlyings(payload, limit=safe_limit)
        response = {
            "query": normalized_query,
            "results": results,
            "page": _page(payload, page_number=safe_page, records=safe_limit),
        }
        _cache_set(cache_key, response, ttl_seconds=60.0)
        return response


def _default_indices(*, limit: int, page_number: int) -> dict[str, Any]:
    start = (page_number - 1) * limit
    end = start + limit
    total_records = len(DEFAULT_OPTION_INDICES)
    total_pages = (total_records + limit - 1) // limit
    return {
        "query": "",
        "results": DEFAULT_OPTION_INDICES[start:end],
        "page": {
            "page_number": page_number,
            "records": limit,
            "total_records": total_records,
            "total_pages": total_pages,
        },
    }


def _shape_underlyings(payload: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        if _string_value(item, "instrument_type") not in {"CE", "PE"}:
            continue
        underlying_type = _string_value(item, "underlying_type")
        if underlying_type not in {"INDEX", "EQUITY"}:
            continue
        underlying_key = _string_value(item, "underlying_key")
        if not underlying_key or underlying_key in seen:
            continue

        seen.add(underlying_key)
        results.append(
            {
                "instrument_key": underlying_key,
                "symbol": _string_value(item, "underlying_symbol"),
                "name": _string_value(item, "name"),
                "underlying_type": underlying_type,
                "exchange": _exchange_from_key(underlying_key) or _string_value(item, "exchange"),
                "lot_size": _number_value(item, "lot_size"),
                "freeze_quantity": _number_value(item, "freeze_quantity"),
                "tick_size": _tick_size(item),
            }
        )
        if len(results) >= limit:
            break

    return results


def _page(payload: dict[str, Any], *, page_number: int, records: int) -> dict[str, int]:
    meta_data = payload.get("meta_data")
    if not isinstance(meta_data, dict):
        return {
            "page_number": page_number,
            "records": records,
            "total_records": len(_shape_underlyings(payload, limit=records)),
            "total_pages": page_number,
        }
    page = meta_data.get("page")
    if not isinstance(page, dict):
        return {
            "page_number": page_number,
            "records": records,
            "total_records": len(_shape_underlyings(payload, limit=records)),
            "total_pages": page_number,
        }
    return {
        "page_number": _int_value(page, "page_number", default=page_number),
        "records": _int_value(page, "records", default=records),
        "total_records": _int_value(page, "total_records", default=0),
        "total_pages": _int_value(page, "total_pages", default=page_number),
    }


def _exchange_from_key(instrument_key: str) -> str:
    prefix = instrument_key.split("|", 1)[0]
    if "_" not in prefix:
        return ""
    return prefix.split("_", 1)[0]


def _string_value(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if isinstance(value, str):
        return value
    return ""


def _number_value(payload: dict[str, Any], name: str) -> float:
    value = payload.get(name)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _tick_size(payload: dict[str, Any]) -> float:
    value = _number_value(payload, "tick_size")
    if value >= 1:
        return value / 100.0
    return value


def _int_value(payload: dict[str, Any], name: str, *, default: int) -> int:
    value = payload.get(name)
    if isinstance(value, int):
        return value
    return default


def _cache_get(key: tuple[str, int, int]) -> Optional[dict[str, Any]]:
    entry = _SEARCH_CACHE.get(key)
    if entry is None:
        return None
    if entry.expires_at <= monotonic():
        _SEARCH_CACHE.pop(key, None)
        return None
    return entry.value


def _cache_set(key: tuple[str, int, int], value: dict[str, Any], *, ttl_seconds: float) -> None:
    _SEARCH_CACHE[key] = _SearchCacheEntry(expires_at=monotonic() + ttl_seconds, value=value)

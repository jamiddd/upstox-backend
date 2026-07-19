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
        "is_optionable": True,
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
        "is_optionable": True,
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
        "is_optionable": True,
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
        "is_optionable": True,
    },
]

# FIX: this search endpoint's underlying query (see UpstoxService.search_instruments) is scoped to
# segments="FO", instrument_types="CE,PE" -- it only ever searches *option contracts* and infers
# the underlying from each one's `underlying_key` field. An index with no listed options at all
# (India VIX -- there's no NSE VIX options/futures market) can therefore never appear in these
# results no matter what's typed, even though it's a perfectly real, quotable NSE index a user
# might reasonably want in their watchlist for reference (volatility context), just not to trade.
# Merged into both the default (empty-query) list and query-matched results in
# search_underlyings() below, confirmed against Upstox's own instrument master directly (not
# guessed) -- same reasoning/precedent as GlobalInstruments.ALL on the Android app side for
# instruments that exist but aren't reachable through the normal option-search path.
# `is_optionable: False` is what lets the app show these as reference-only, non-selectable
# entries instead of handing a non-optionable instrument key to the Main screen's bootstrap call.
NON_OPTIONABLE_INDICES = [
    {
        "instrument_key": "NSE_INDEX|India VIX",
        "symbol": "INDIA VIX",
        "name": "India VIX",
        "underlying_type": "INDEX",
        "exchange": "NSE",
        "lot_size": 0.0,
        "freeze_quantity": 0.0,
        "tick_size": 0.0,
        "is_optionable": False,
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
        include_futures: bool = False,
    ) -> dict[str, Any]:
        """Return deduped index/equity underlyings that have option contracts.

        [include_futures] additionally merges in matching current-month futures contracts
        (instrument_type FUT) -- see _shape_futures' doc comment for why these need their own
        opt-in flag rather than always being included. Only the Watchlist "Add" flow (not the
        Search screen's underlying picker, which needs a real option-capable underlying to hand
        off to the Main screen's bootstrap call) should ever pass this true.
        """
        normalized_query = query.strip()
        safe_limit = min(max(limit, 1), 30)
        safe_page = max(page_number, 1)
        if not normalized_query:
            return _default_indices(limit=safe_limit, page_number=safe_page)

        cache_key = (normalized_query.upper(), safe_limit, safe_page, include_futures)
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
        # See NON_OPTIONABLE_INDICES's doc comment -- these can never come back from the F&O
        # search above, so they're matched against the query text here and merged in directly.
        # Only on page 1 (they're a small fixed list, not worth paginating), and only far enough
        # to not push safe_limit -- a real Upstox match still wins the remaining slots.
        if safe_page == 1:
            matches = _matching_non_optionable_indices(normalized_query)
            seen_keys = {item["instrument_key"] for item in results}
            for item in matches:
                if len(results) >= safe_limit:
                    break
                if item["instrument_key"] in seen_keys:
                    continue
                results.append(item)
                seen_keys.add(item["instrument_key"])
            if include_futures and len(results) < safe_limit:
                futures_payload = await self.upstox.search_instruments(
                    access_token,
                    query=normalized_query,
                    instrument_types="FUT",
                    page_number=1,
                    records=safe_limit - len(results),
                )
                for item in _shape_futures(futures_payload, limit=safe_limit - len(results)):
                    if item["instrument_key"] in seen_keys:
                        continue
                    results.append(item)
                    seen_keys.add(item["instrument_key"])
        response = {
            "query": normalized_query,
            "results": results,
            "page": _page(payload, page_number=safe_page, records=safe_limit),
        }
        _cache_set(cache_key, response, ttl_seconds=60.0)
        return response


def _matching_non_optionable_indices(query: str) -> list[dict[str, Any]]:
    normalized = query.upper()
    return [
        item
        for item in NON_OPTIONABLE_INDICES
        if normalized in item["symbol"].upper() or normalized in item["name"].upper()
    ]


def _default_indices(*, limit: int, page_number: int) -> dict[str, Any]:
    all_indices = DEFAULT_OPTION_INDICES + NON_OPTIONABLE_INDICES
    start = (page_number - 1) * limit
    end = start + limit
    total_records = len(all_indices)
    total_pages = (total_records + limit - 1) // limit
    return {
        "query": "",
        "results": all_indices[start:end],
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
                # These only ever come from a live CE/PE search result (see this function's own
                # segments="FO" scoping), so the underlying they point to is optionable by
                # construction.
                "is_optionable": True,
            }
        )
        if len(results) >= limit:
            break

    return results


def _shape_futures(payload: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    """Shapes instrument_types=FUT search results as their own watchlist-only entries.

    Unlike _shape_underlyings (which discards each CE/PE result and keeps only the *underlying*
    it points to, via underlying_key/underlying_symbol), a futures contract IS the instrument to
    watch -- there's no separate "underlying" indirection to resolve. underlying_type is
    deliberately set to "FUTURES" (not a real Upstox value) so the Android app can tell these
    apart from a real option-capable underlying if it ever needs to -- e.g. these are meaningless
    to hand to the Main screen's bootstrap call (which expects an INDEX/EQUITY underlying key with
    option contracts), only good for watching a live price.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        if _string_value(item, "instrument_type") != "FUT":
            continue
        instrument_key = _string_value(item, "instrument_key")
        if not instrument_key or instrument_key in seen:
            continue

        seen.add(instrument_key)
        results.append(
            {
                "instrument_key": instrument_key,
                "symbol": _string_value(item, "trading_symbol"),
                "name": _string_value(item, "name"),
                "underlying_type": "FUTURES",
                "exchange": _exchange_from_key(instrument_key) or _string_value(item, "exchange"),
                "lot_size": _number_value(item, "lot_size"),
                "freeze_quantity": _number_value(item, "freeze_quantity"),
                "tick_size": _tick_size(item),
                # A futures contract IS the instrument, not an underlying with its own separate
                # option chain -- same "meaningless to hand to the Main screen's bootstrap call"
                # reasoning as NON_OPTIONABLE_INDICES above.
                "is_optionable": False,
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

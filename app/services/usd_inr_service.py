from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, Optional

import httpx

# Yahoo Finance's unofficial chart endpoint -- no API key, no auth, but also undocumented and
# unofficial: it could change shape or start rate-limiting without notice. Used here only because
# Upstox's own quotes/LTP endpoints reject USD INR outright (see AppSettingsRepository.kt's
# GlobalInstruments doc comment on the Android side) and the user explicitly doesn't need this to
# be accurate/official, just roughly current.
_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/USDINR=X"

# A bare request with no User-Agent gets blocked by Yahoo -- confirmed live.
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Throttles actual outbound Yahoo requests to at most once a minute, regardless of how often the
# Android app (or anything else) polls this service -- worth being conservative with an unofficial
# endpoint, unlike Upstox's own (paid, rate-limit-documented) API.
_CACHE_TTL_SECONDS = 60.0
_CACHE_KEY = "usd_inr_quote"


@dataclass
class _CacheEntry:
    expires_at: float
    value: Any


_CACHE: dict[str, _CacheEntry] = {}


def _cache_get(key: str) -> Optional[Any]:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    if entry.expires_at <= monotonic():
        _CACHE.pop(key, None)
        return None
    return entry.value


def _cache_set(key: str, value: Any, *, ttl_seconds: float) -> None:
    _CACHE[key] = _CacheEntry(expires_at=monotonic() + ttl_seconds, value=value)


class UsdInrService:
    """Best-effort USD/INR quote from a free non-Upstox source (see [_YAHOO_CHART_URL]'s doc
    comment for why). Named after the domain concern, not the vendor, since Yahoo is an
    implementation detail that could get swapped out later if this endpoint ever breaks for good.

    [get_quote] never raises -- any failure (network error, unexpected response shape, rate limit)
    degrades to `None`, since this is a "nice to have" ticker entry, not core trading data, and
    there's no documented/enumerable exception shape from an unofficial endpoint to catch more
    precisely than a bare `except Exception`.
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client

    async def get_quote(self) -> Optional[dict[str, float]]:
        cached = _cache_get(_CACHE_KEY)
        if cached is not None:
            return cached

        quote = await self._fetch()
        if quote is not None:
            _cache_set(_CACHE_KEY, quote, ttl_seconds=_CACHE_TTL_SECONDS)
        return quote

    async def _fetch(self) -> Optional[dict[str, float]]:
        try:
            if self._client is not None:
                response = await self._client.get(
                    _YAHOO_CHART_URL,
                    params={"range": "1d", "interval": "1d"},
                    headers=_HEADERS,
                )
            else:
                async with httpx.AsyncClient(timeout=10.0) as scoped_client:
                    response = await scoped_client.get(
                        _YAHOO_CHART_URL,
                        params={"range": "1d", "interval": "1d"},
                        headers=_HEADERS,
                    )
            response.raise_for_status()
            meta = response.json()["chart"]["result"][0]["meta"]
            # meta.previousClose is sometimes absent with range=1d&interval=1d (confirmed live) --
            # chartPreviousClose is the reliable fallback.
            previous_close = meta.get("previousClose")
            if previous_close is None:
                previous_close = meta["chartPreviousClose"]
            return {
                "ltp": float(meta["regularMarketPrice"]),
                "previous_close": float(previous_close),
            }
        except Exception:
            return None

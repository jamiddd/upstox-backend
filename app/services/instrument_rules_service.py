from __future__ import annotations

from dataclasses import dataclass
import gzip
import json
import math
from time import monotonic
from typing import Any, Optional

import httpx

from app.core.config import Settings
from app.core.exceptions import AppConfigError


@dataclass(frozen=True)
class InstrumentRules:
    """Exchange constraints for an instrument."""

    instrument_key: str
    lot_size: int
    freeze_quantity: int
    tick_size: float
    trading_symbol: str


@dataclass
class _MasterCache:
    expires_at: float
    by_key: dict[str, dict[str, Any]]


_CACHE: Optional[_MasterCache] = None


class InstrumentRulesService:
    """Lookup instrument constraints from Upstox BOD instruments."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Optional[httpx.AsyncClient] = None,
        ttl_seconds: float = 86400.0,
    ) -> None:
        self.settings = settings
        self._client = client
        self.ttl_seconds = ttl_seconds

    async def get_rules(self, instrument_key: str) -> InstrumentRules:
        """Return lot, freeze, and tick rules for an instrument key."""
        master = await self._master()
        row = master.get(instrument_key)
        if row is None:
            raise AppConfigError(f"Instrument rules not found for {instrument_key}")

        return InstrumentRules(
            instrument_key=instrument_key,
            lot_size=max(_int_value(row, "lot_size"), 1),
            freeze_quantity=max(_int_value(row, "freeze_quantity"), 1),
            tick_size=_normalized_tick_size(_number_value(row, "tick_size")),
            trading_symbol=_string_value(row, "trading_symbol"),
        )

    async def _master(self) -> dict[str, dict[str, Any]]:
        global _CACHE
        if _CACHE is not None and _CACHE.expires_at > monotonic():
            return _CACHE.by_key

        rows = await self._fetch_master_rows()
        by_key = {
            item["instrument_key"]: item
            for item in rows
            if isinstance(item, dict) and isinstance(item.get("instrument_key"), str)
        }
        _CACHE = _MasterCache(expires_at=monotonic() + self.ttl_seconds, by_key=by_key)
        return by_key

    async def _fetch_master_rows(self) -> list[dict[str, Any]]:
        client = self._client
        if client is not None:
            response = await client.get(self.settings.upstox_instrument_master_url)
        else:
            async with httpx.AsyncClient(timeout=30.0) as scoped_client:
                response = await scoped_client.get(self.settings.upstox_instrument_master_url)

        response.raise_for_status()
        content = response.content
        if self.settings.upstox_instrument_master_url.endswith(".gz"):
            content = gzip.decompress(content)
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, list):
            raise AppConfigError("Upstox instrument master did not return a list")
        return [item for item in payload if isinstance(item, dict)]


def validate_quantity(quantity: int, rules: InstrumentRules) -> None:
    """Validate quantity against lot size."""
    if quantity % rules.lot_size != 0:
        raise AppConfigError(
            f"Quantity {quantity} must be a multiple of lot size {rules.lot_size}"
        )


def validate_price(price: float, rules: InstrumentRules, *, field_name: str) -> None:
    """Validate price against tick size."""
    if rules.tick_size <= 0:
        return
    ratio = price / rules.tick_size
    if not math.isclose(ratio, round(ratio), abs_tol=1e-7):
        raise AppConfigError(
            f"{field_name} {price} must be a multiple of tick size {rules.tick_size}"
        )


def slice_quantity_for_freeze(quantity: int, rules: InstrumentRules) -> int:
    """Use exchange freeze quantity as the preferred slice size."""
    return min(quantity, rules.freeze_quantity)


def _normalized_tick_size(raw_tick_size: float) -> float:
    if raw_tick_size <= 0:
        return 0.0
    if raw_tick_size >= 1:
        return raw_tick_size / 100.0
    return raw_tick_size


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


def _int_value(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, (int, float)):
        return int(value)
    return 0

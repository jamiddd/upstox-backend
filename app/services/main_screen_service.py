from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, Optional

from app.core.exceptions import UpstoxApiError
from app.services.upstox_service import UpstoxService

DEFAULT_UNDERLYING_KEY = "NSE_INDEX|Nifty 50"
DEFAULT_UNDERLYING_SYMBOL = "NIFTY"
DEFAULT_UNDERLYING_NAME = "NIFTY 50"


@dataclass
class _CacheEntry:
    expires_at: float
    value: dict[str, Any]


_CACHE: dict[tuple[Any, ...], _CacheEntry] = {}


class MainScreenService:
    """Build screen-ready payloads for the option trading main screen."""

    def __init__(self, upstox_service: UpstoxService) -> None:
        self.upstox = upstox_service

    async def bootstrap(
        self,
        access_token: str,
        *,
        underlying_key: str = DEFAULT_UNDERLYING_KEY,
        expiry_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return initial data needed to render the main screen."""
        contracts = await self._option_contracts(access_token, underlying_key)
        expiries = _extract_expiries(contracts)
        selected_expiry = expiry_date or (expiries[0] if expiries else None)
        quote = await self._quotes(access_token, [underlying_key])
        summary = await self.summary(access_token)
        positions = await self._positions(access_token)

        return {
            "underlying": {
                "instrument_key": underlying_key,
                "symbol": _underlying_symbol(underlying_key, contracts),
                "name": _underlying_name(underlying_key, contracts),
                "spot_price": _last_price(_find_quote(quote, underlying_key)),
            },
            "expiries": expiries,
            "selected_expiry": selected_expiry,
            "summary": summary,
            "open_positions": [_shape_position(position) for position in positions],
        }

    async def selected_quote(
        self,
        access_token: str,
        *,
        underlying_key: str,
        expiry_date: str,
        strike_price: float,
        option_type: str,
    ) -> dict[str, Any]:
        """Return bid/ask-ready quote data for the app-selected option strike."""
        contract = await self._resolve_contract(
            access_token,
            underlying_key=underlying_key,
            expiry_date=expiry_date,
            strike_price=strike_price,
            option_type=option_type,
        )
        contract_key = _string_value(contract, "instrument_key")
        quotes = await self._quotes(access_token, [underlying_key, contract_key])
        underlying_quote = _find_quote(quotes, underlying_key)
        contract_quote = _find_quote(quotes, contract_key)

        return {
            "underlying": {
                "instrument_key": underlying_key,
                "spot_price": _last_price(underlying_quote),
            },
            "contract": {
                "instrument_key": contract_key,
                "trading_symbol": _string_value(contract, "trading_symbol", "tradingsymbol"),
                "strike_price": _number_value(contract, "strike_price"),
                "option_type": _option_type(contract),
                "lot_size": _number_value(contract, "lot_size"),
                "freeze_quantity": _number_value(contract, "freeze_quantity"),
                "tick_size": _tick_size(contract),
                "ltp": _last_price(contract_quote),
                "bid_price": _best_depth_price(contract_quote, "buy"),
                "ask_price": _best_depth_price(contract_quote, "sell"),
            },
        }

    async def option_chain(
        self,
        access_token: str,
        *,
        underlying_key: str,
        expiry_date: str,
    ) -> dict[str, Any]:
        """Return every strike's CE/PE contract metadata for one underlying + expiry.

        Clients use this to figure out the real strike interval and the at-the-money
        strike themselves, from the actual listed strikes -- there's no reliable way to
        guess a strike step (it varies by underlying: 50 for NIFTY, 100 for BANKNIFTY,
        arbitrary for stocks) without seeing the real option chain.

        Unlike `selected_quote`, this does not fetch live bid/ask quotes for every
        strike (that would be one Upstox quote call per strike times two, on every
        expiry change) -- it only returns contract metadata (instrument keys, lot
        size, tick size) needed to resolve which contract is ATM. The client then
        calls `selected_quote` for that one strike to get its live price.
        """
        contracts = await self._option_contracts(
            access_token,
            underlying_key,
            expiry_date=expiry_date,
        )
        by_strike: dict[float, dict[str, Any]] = {}
        for contract in _contracts_data(contracts):
            if contract.get("expiry") != expiry_date:
                continue
            option_type = _option_type(contract)
            if option_type not in ("CE", "PE"):
                continue
            strike = _number_value(contract, "strike_price")
            entry = by_strike.setdefault(strike, {"strike_price": strike})
            entry[option_type.lower()] = {
                "instrument_key": _string_value(contract, "instrument_key"),
                "trading_symbol": _string_value(contract, "trading_symbol", "tradingsymbol"),
                "lot_size": _number_value(contract, "lot_size"),
                "freeze_quantity": _number_value(contract, "freeze_quantity"),
                "tick_size": _tick_size(contract),
            }

        strikes = sorted(by_strike.values(), key=lambda item: item["strike_price"])
        return {
            "underlying_key": underlying_key,
            "expiry_date": expiry_date,
            "strikes": strikes,
        }

    async def position_quotes(
        self,
        access_token: str,
        *,
        instrument_keys: list[str],
    ) -> dict[str, Any]:
        """Return LTP snapshots for currently open position instruments."""
        keys = _dedupe([key for key in instrument_keys if key])
        if not keys:
            return {"positions": []}

        quotes = await self._quotes(access_token, keys)
        return {
            "positions": [
                {
                    "instrument_key": key,
                    "ltp": _last_price(_find_quote(quotes, key)),
                }
                for key in keys
            ]
        }

    async def summary(self, access_token: str) -> dict[str, float]:
        """Return the balance/margin/P&L summary for the screen.

        `opening_balance`/`profit_loss`/`closing_balance` are the original three fields (kept
        exactly as before for backward compatibility) -- despite the name, `opening_balance` is
        actually re-fetched live on every call (only cached for 5s), so it already reflects
        same-day fund additions/withdrawals; it just isn't *labeled* that way to the user.

        `available_margin`/`margin_used`/`payin_amount` are new: pulled from the same funds
        payload via `_find_numeric_field`, a tolerant recursive search rather than a hardcoded
        path -- Upstox's V3 funds response isn't fully documented/verified against a live account
        in this codebase (only the `opening_balance` leaf was previously confirmed), so a search
        by field name is safer than guessing an exact nesting that might silently return nothing.
        Each falls back to a sensible default (never null) if Upstox doesn't include that field
        for this account.
        """
        cache_key = ("summary",)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        funds_payload = await self.upstox.get_funds_and_margin(access_token)
        positions_payload = await self.upstox.get_positions(access_token)
        opening_balance = _opening_balance(funds_payload)
        profit_loss = _positions_pnl(_positions_data(positions_payload))

        available_margin = _find_numeric_field(funds_payload, "available_margin")
        margin_used = _find_numeric_field(funds_payload, "used_margin")
        payin_amount = _find_numeric_field(funds_payload, "payin_amount")

        summary = {
            "opening_balance": opening_balance,
            "profit_loss": profit_loss,
            "closing_balance": opening_balance + profit_loss,
            # Falls back to opening_balance (today's live cash, pre-margin-block) if Upstox
            # doesn't surface a dedicated available_margin field for this account/segment.
            "available_margin": available_margin if available_margin is not None else opening_balance,
            "margin_used": margin_used if margin_used is not None else 0.0,
            "payin_amount": payin_amount if payin_amount is not None else 0.0,
        }
        _cache_set(cache_key, summary, ttl_seconds=5.0)
        return summary

    async def _resolve_contract(
        self,
        access_token: str,
        *,
        underlying_key: str,
        expiry_date: str,
        strike_price: float,
        option_type: str,
    ) -> dict[str, Any]:
        contracts = await self._option_contracts(
            access_token,
            underlying_key,
            expiry_date=expiry_date,
        )
        target_type = option_type.upper()
        for contract in _contracts_data(contracts):
            if _option_type(contract) != target_type:
                continue
            if _same_price(_number_value(contract, "strike_price"), strike_price):
                return contract

        raise UpstoxApiError(
            "Option contract not found for selected strike",
            status_code=404,
            details={
                "underlying_key": underlying_key,
                "expiry_date": expiry_date,
                "strike_price": strike_price,
                "option_type": target_type,
            },
        )

    async def _option_contracts(
        self,
        access_token: str,
        underlying_key: str,
        *,
        expiry_date: Optional[str] = None,
    ) -> dict[str, Any]:
        cache_key = ("contracts", underlying_key, expiry_date or "")
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        payload = await self.upstox.get_option_contracts(
            access_token,
            underlying_key,
            expiry_date=expiry_date,
        )
        _cache_set(cache_key, payload, ttl_seconds=600.0)
        return payload

    async def _quotes(self, access_token: str, instrument_keys: list[str]) -> dict[str, Any]:
        keys = _dedupe(instrument_keys)
        cache_key = ("quotes", ",".join(keys))
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        payload = await self.upstox.get_quotes(access_token, ",".join(keys))
        _cache_set(cache_key, payload, ttl_seconds=0.75)
        return payload

    async def _positions(self, access_token: str) -> list[dict[str, Any]]:
        cache_key = ("positions",)
        cached = _cache_get(cache_key)
        if cached is not None:
            return _positions_data(cached)

        payload = await self.upstox.get_positions(access_token)
        _cache_set(cache_key, payload, ttl_seconds=1.0)
        return _positions_data(payload)


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


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _contracts_data(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _positions_data(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    positions = [item for item in data if isinstance(item, dict)]
    return [position for position in positions if _number_value(position, "quantity") != 0]


def _extract_expiries(payload: dict[str, Any]) -> list[str]:
    expiries = {
        expiry
        for contract in _contracts_data(payload)
        if isinstance((expiry := contract.get("expiry")), str)
    }
    return sorted(expiries)


def _find_quote(payload: dict[str, Any], instrument_key: str) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}

    direct = data.get(instrument_key)
    if isinstance(direct, dict):
        return direct

    for quote in data.values():
        if not isinstance(quote, dict):
            continue
        if quote.get("instrument_token") == instrument_key:
            return quote
    return {}


def _shape_position(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "instrument_key": _string_value(position, "instrument_token", "instrument_key"),
        "trading_symbol": _string_value(position, "trading_symbol", "tradingsymbol"),
        "quantity": _number_value(position, "quantity"),
        "entry_price": _entry_price(position),
        "last_price": _number_value(position, "last_price"),
        "pnl": _number_value(position, "pnl"),
    }


def _entry_price(position: dict[str, Any]) -> float:
    average_price = position.get("average_price")
    if isinstance(average_price, (int, float)):
        return float(average_price)
    buy_price = position.get("buy_price")
    if isinstance(buy_price, (int, float)):
        return float(buy_price)
    sell_price = position.get("sell_price")
    if isinstance(sell_price, (int, float)):
        return float(sell_price)
    return 0.0


def _opening_balance(payload: dict[str, Any]) -> float:
    data = payload.get("data")
    if not isinstance(data, dict):
        return 0.0
    available = data.get("available_to_trade")
    if not isinstance(available, dict):
        return 0.0
    cash_available = available.get("cash_available_to_trade")
    if not isinstance(cash_available, dict):
        return 0.0
    cash = cash_available.get("cash")
    if not isinstance(cash, dict):
        return 0.0
    return _number_value(cash, "opening_balance")


def _positions_pnl(positions: list[dict[str, Any]]) -> float:
    return sum(_number_value(position, "pnl") for position in positions)


def _find_numeric_field(payload: Any, field_name: str) -> Optional[float]:
    """Recursively searches `payload` for the first numeric value at a key named
    `field_name`, at any depth (dicts and lists). Used for funds/margin fields whose exact
    nesting in Upstox's V3 response isn't confirmed -- a name-based search degrades gracefully
    (returns None) if the field is missing or the shape differs, rather than a hardcoded `.get()`
    chain that would just as silently return nothing but look like a real, verified path.
    """
    if isinstance(payload, dict):
        value = payload.get(field_name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        for nested in payload.values():
            found = _find_numeric_field(nested, field_name)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_numeric_field(item, field_name)
            if found is not None:
                return found
    return None


def _underlying_symbol(underlying_key: str, contracts_payload: dict[str, Any]) -> str:
    for contract in _contracts_data(contracts_payload):
        symbol = _string_value(contract, "underlying_symbol", "name")
        if symbol:
            return symbol
    if underlying_key == DEFAULT_UNDERLYING_KEY:
        return DEFAULT_UNDERLYING_SYMBOL
    return underlying_key.split("|")[-1]


def _underlying_name(underlying_key: str, contracts_payload: dict[str, Any]) -> str:
    for contract in _contracts_data(contracts_payload):
        name = _string_value(contract, "name", "underlying_symbol")
        if name:
            return name
    if underlying_key == DEFAULT_UNDERLYING_KEY:
        return DEFAULT_UNDERLYING_NAME
    return underlying_key.split("|")[-1]


def _option_type(contract: dict[str, Any]) -> str:
    return _string_value(contract, "instrument_type", "option_type").upper()


def _last_price(quote: dict[str, Any]) -> float:
    return _number_value(quote, "last_price", "ltp")


def _best_depth_price(quote: dict[str, Any], side: str) -> float:
    depth = quote.get("depth")
    if not isinstance(depth, dict):
        return 0.0
    levels = depth.get(side)
    if not isinstance(levels, list) or not levels:
        return 0.0
    first = levels[0]
    if not isinstance(first, dict):
        return 0.0
    return _number_value(first, "price")


def _string_value(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str):
            return value
    return ""


def _number_value(payload: dict[str, Any], *names: str) -> float:
    for name in names:
        value = payload.get(name)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _tick_size(payload: dict[str, Any]) -> float:
    value = _number_value(payload, "tick_size")
    if value >= 1:
        return value / 100.0
    return value


def _same_price(left: float, right: float) -> bool:
    return abs(left - right) < 0.0001

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

        Field shapes below are confirmed against a real `GET /v3/user/get-funds-and-margin`
        response (see `docs/MAIN_SCREEN_API.md`'s "Raw Funds and Margin" passthrough route) --
        not guessed:

        - `opening_balance` (`available_to_trade.cash_available_to_trade.cash.opening_balance`)
          really is a static start-of-day snapshot -- it does NOT move when cash is added/
          withdrawn intraday. Kept only because `closing_balance` is defined in terms of it.
        - `available_margin` (`available_to_trade.total`) is the true "can I place another order
          right now" number: cash + pledge margin, already net of margin blocked by open
          positions and today's cash additions/withdrawals.
        - `margin_used` sums `cash_available_to_trade.margin_used.total` and
          `pledge_available_to_trade.margin_used.total` -- margin currently locked by open
          positions, from both cash and pledged collateral.
        - `payin_amount` is `cash.added_today + cash.withdrawn_today` (the latter already
          negative) -- net cash movement today, which is exactly the "I added money mid-day"
          case `available_margin`/`closing_balance` should (and now do) reflect.
        - `closing_balance` = `opening_balance + payin_amount + profit_loss` -- extended from the
          original `opening_balance + profit_loss` to also account for today's net cash
          movement, since a mid-day deposit is real money added to the account, not "profit".
        """
        cache_key = ("summary",)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        funds_payload = await self.upstox.get_funds_and_margin(access_token)
        positions_payload = await self.upstox.get_positions(access_token)
        opening_balance = _opening_balance(funds_payload)
        # _all_positions_data, not _positions_data -- the day's total P&L must include positions
        # already squared off today (quantity 0), which _positions_data deliberately filters out
        # for *display* purposes. Using the filtered list here was the bug that made "Today's
        # P&L" read as 0 on a day with lots of open-and-close scalps.
        profit_loss = _positions_pnl(_all_positions_data(positions_payload))

        available_margin = _available_margin_total(funds_payload)
        margin_used = _margin_used_total(funds_payload)
        payin_amount = _net_cash_added_today(funds_payload)

        summary = {
            "opening_balance": opening_balance,
            "profit_loss": profit_loss,
            "closing_balance": opening_balance + payin_amount + profit_loss,
            "available_margin": available_margin,
            "margin_used": margin_used,
            "payin_amount": payin_amount,
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
    """Positions to *display* as "open positions" -- filters out anything already squared off
    (quantity 0) today. NOT what the day's total P&L should be summed over -- see
    `_all_positions_data`/`summary()`, since Upstox still reports a squared-off position's
    realized P&L for the day here even though its quantity is now 0, and this filter would
    silently drop it.
    """
    return [position for position in _all_positions_data(payload) if _number_value(position, "quantity") != 0]


def _all_positions_data(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every position Upstox returned for today, open or already squared off -- use this (not
    `_positions_data`) for anything that needs the day's *total* P&L, since a fully closed
    position still carries its realized P&L here even though its quantity is 0.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


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


def _available_to_trade(payload: dict[str, Any]) -> dict[str, Any]:
    """The `data.available_to_trade` object shared by every funds/margin field below -- see
    `docs/MAIN_SCREEN_API.md`'s "Raw Funds and Margin" section for the confirmed real shape.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    available = data.get("available_to_trade")
    return available if isinstance(available, dict) else {}


def _cash_block(payload: dict[str, Any]) -> dict[str, Any]:
    cash_available = _available_to_trade(payload).get("cash_available_to_trade")
    if not isinstance(cash_available, dict):
        return {}
    cash = cash_available.get("cash")
    return cash if isinstance(cash, dict) else {}


def _opening_balance(payload: dict[str, Any]) -> float:
    """A genuine start-of-day snapshot (confirmed against a live response -- it does NOT move
    when cash is added/withdrawn intraday, unlike what an earlier pass here assumed).
    """
    return _number_value(_cash_block(payload), "opening_balance")


def _net_cash_added_today(payload: dict[str, Any]) -> float:
    """`added_today + withdrawn_today` (the latter already negative in Upstox's response) --
    net cash movement today, e.g. +110 added and -130 withdrawn nets to -20.
    """
    cash = _cash_block(payload)
    return _number_value(cash, "added_today") + _number_value(cash, "withdrawn_today")


def _available_margin_total(payload: dict[str, Any]) -> float:
    """`available_to_trade.total` -- cash + pledge margin, already net of margin blocked by open
    positions and today's cash movement. The actual "can I place another order right now" number.
    """
    return _number_value(_available_to_trade(payload), "total")


def _margin_used_total(payload: dict[str, Any]) -> float:
    """Margin currently locked by open positions, summed across both cash and pledged
    collateral (`cash_available_to_trade.margin_used.total` + `pledge_available_to_trade
    .margin_used.total`).
    """
    available = _available_to_trade(payload)
    total = 0.0
    for segment_key in ("cash_available_to_trade", "pledge_available_to_trade"):
        segment = available.get(segment_key)
        if not isinstance(segment, dict):
            continue
        margin_used = segment.get("margin_used")
        if isinstance(margin_used, dict):
            total += _number_value(margin_used, "total")
    return total


def _positions_pnl(positions: list[dict[str, Any]]) -> float:
    return sum(_number_value(position, "pnl") for position in positions)


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

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from time import monotonic
from typing import Any, Optional

from app.core.exceptions import UpstoxApiError
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

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
                # Previous trading day's close -- lets the app show a "(+0.40%)" change badge
                # next to the spot price. Fetched directly from the daily candle endpoint (the
                # previous *completed* session's own close), not derived from a live quote's
                # `net_change` field -- see _fetch_previous_close's doc comment for why.
                "previous_close": await self._fetch_previous_close(access_token, underlying_key, date.today()),
            },
            "expiries": expiries,
            "selected_expiry": selected_expiry,
            "summary": summary,
            "open_positions": [_shape_position(position) for position in positions],
        }

    async def resolve_underlying_symbol_and_expiry(
        self,
        access_token: str,
        underlying_key: str,
    ) -> tuple[str, Optional[str]]:
        """Returns `(underlying_symbol, nearest_expiry)` for [underlying_key] -- the same symbol
        text and "nearest listed expiry" (`expiries[0]`) [bootstrap] resolves for a fresh Main
        screen load, exposed standalone for the tracked-instruments background poller (see
        `app.services.tracked_instruments_poller`), which needs both to call
        `UnderlyingSignalsService.get_signals` for a Settings-picked underlying_key without a
        live client request ever having supplied them. `nearest_expiry` is `None` if this
        underlying has no listed option contracts at all (mirrors `bootstrap`'s own fallback).
        Reuses [_option_contracts]'s own 600s cache -- cheap even if `bootstrap` was already
        called for the same underlying recently.
        """
        contracts = await self._option_contracts(access_token, underlying_key)
        expiries = _extract_expiries(contracts)
        return _underlying_symbol(underlying_key, contracts), (expiries[0] if expiries else None)

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
        """Return every strike's live CE/PE market data + greeks for one underlying + expiry --
        powers the app's smart strike selector (ATM/delta-target/liquidity/manual-offset/
        DTE-aware modes all pick from this same per-strike data, client-side).

        FIX: this used to call Upstox's `/option/contract` endpoint, which only returns bare
        contract metadata (instrument key, lot size, tick size) -- no LTP, no bid/ask, no OI, no
        greeks, so there was no data to build anything "smart" from. Upstox's `/option/chain`
        endpoint returns everything needed for every strike in one call: LTP/volume/OI/bid-ask
        (`market_data`) and delta/gamma/theta/vega/iv (`option_greeks`), for both call_options and
        put_options -- see `UpstoxService.get_option_chain`. Cached far more briefly than the old
        per-contract metadata (see `_option_chain_live`) since this data is live-changing
        (LTP/OI/greeks drift all day), not static.
        """
        payload = await self._option_chain_live(access_token, underlying_key, expiry_date=expiry_date)
        rows = payload.get("data")
        if not isinstance(rows, list):
            return {"underlying_key": underlying_key, "expiry_date": expiry_date, "strikes": []}

        underlying_spot_price = 0.0
        strikes: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if underlying_spot_price == 0.0:
                underlying_spot_price = _number_value(row, "underlying_spot_price")
            strikes.append(
                {
                    "strike_price": _number_value(row, "strike_price"),
                    "ce": _option_side(row.get("call_options")),
                    "pe": _option_side(row.get("put_options")),
                }
            )
        strikes.sort(key=lambda item: item["strike_price"])

        return {
            "underlying_key": underlying_key,
            "expiry_date": expiry_date,
            "underlying_spot_price": underlying_spot_price,
            "strikes": strikes,
        }

    async def position_quotes(
        self,
        access_token: str,
        *,
        instrument_keys: list[str],
    ) -> dict[str, Any]:
        """Return LTP (+ previous close) snapshots for any instrument keys.

        Originally written for open positions, but this is really just a generic
        "give me a quote for these instrument keys" call -- the app also uses it to poll the
        Main screen toolbar's watchlist ticker (both the regular NSE/BSE watchlist and the
        Global Instruments ticker, e.g. `GLOBAL_INDEX|^GSPC`/`GLOBAL_INDICATOR|USDINR` -- see
        Upstox's Global Instruments file, which the Full Market Quote endpoint this wraps
        (`_quotes`/`get_quotes`) supports directly). `previous_close` lets the app color each
        entry by direction (up/down vs. yesterday's close) the same way the underlying's own
        spot-price change badge does.
        """
        keys = _dedupe([key for key in instrument_keys if key])
        if not keys:
            return {"positions": []}

        # FIX: a single instrument Upstox doesn't actually recognize (confirmed for its
        # GLOBAL_INDICATOR segment -- USDINR/BZUSD/CLUSD are listed in Upstox's own Global
        # Instruments file but rejected by both the quotes and LTP endpoints with "One of either
        # symbol or instrument_key is invalid") makes Upstox reject the *entire* batched request,
        # not just that key -- so every other instrument in the same call (e.g. the Global
        # ticker's otherwise-working GIFT NIFTY/S&P 500/etc.) silently got no data too. Falling
        # back to fetching one instrument at a time when the batch fails means one bad key only
        # blanks itself out, not everyone requested alongside it.
        try:
            quotes = await self._quotes(access_token, keys)
        except UpstoxApiError:
            quotes = {"data": {}}
            for key in keys:
                try:
                    single = await self._quotes(access_token, [key])
                except UpstoxApiError:
                    logger.warning("position_quotes: Upstox rejected instrument key %s", key)
                    continue
                quotes["data"].update(single.get("data") or {})

        today = date.today()
        positions = []
        for key in keys:
            quote = _find_quote(quotes, key)
            if not quote:
                # Upstox keys its quotes response by a colon-separated "EXCHANGE:SYMBOL" form,
                # not the pipe-separated instrument_key used in the request, so _find_quote's
                # fallback match relies on each quote's own "instrument_token" field lining up
                # with what was requested -- log the raw response keys here so a mismatch is
                # visible in `docker compose logs` instead of just showing "--".
                logger.warning(
                    "position_quotes: no quote found for %s -- response data keys: %s",
                    key,
                    list(quotes.get("data", {}).keys()) if isinstance(quotes.get("data"), dict) else quotes.get("data"),
                )
            positions.append(
                {
                    "instrument_key": key,
                    "ltp": _last_price(quote),
                    "previous_close": await self._fetch_previous_close(access_token, key, today),
                }
            )
        return {"positions": positions}

    async def summary(self, access_token: str) -> dict[str, Any]:
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

        FIX: Upstox's funds-and-margin endpoint has a documented daily maintenance window
        (~12:00 AM - 5:30 AM IST, Upstox error code UDAPI100072, "The Funds service is
        accessible from 5:30 AM to 12:00 AM IST daily") during which it reliably fails. That
        used to take down this *entire* bootstrap call (spot price, expiries, positions are all
        otherwise independently available) since the exception wasn't caught here and propagated
        all the way up as a 423. Now only the funds-derived fields degrade (to 0, with
        `funds_unavailable_note` explaining why) -- the rest of the screen still loads.
        """
        cache_key = ("summary",)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        funds_payload: dict[str, Any] = {}
        funds_unavailable_note: Optional[str] = None
        try:
            funds_payload = await self.upstox.get_funds_and_margin(access_token)
        except UpstoxApiError as exc:
            funds_unavailable_note = exc.message or "Funds and margin data is temporarily unavailable."

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

        summary: dict[str, Any] = {
            "opening_balance": opening_balance,
            "profit_loss": profit_loss,
            "closing_balance": opening_balance + payin_amount + profit_loss,
            "available_margin": available_margin,
            "margin_used": margin_used,
            "payin_amount": payin_amount,
            "funds_unavailable_note": funds_unavailable_note,
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

    async def _option_chain_live(
        self,
        access_token: str,
        underlying_key: str,
        *,
        expiry_date: str,
    ) -> dict[str, Any]:
        # A distinct cache key/namespace from _option_contracts above -- and a much shorter TTL
        # (15s, not 600s), since this call's LTP/OI/greeks genuinely drift throughout the day,
        # unlike _option_contracts' static instrument metadata.
        cache_key = ("option_chain_live", underlying_key, expiry_date)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        payload = await self.upstox.get_option_chain(access_token, underlying_key, expiry_date=expiry_date)
        _cache_set(cache_key, payload, ttl_seconds=15.0)
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

    async def _fetch_previous_close(self, access_token: str, instrument_key: str, today: date) -> float:
        """The previous *completed* trading session's actual closing price for [instrument_key].

        FIX: this used to be derived from a live quote's `net_change` field (`last_price -
        net_change`), on the assumption that `net_change` is always the signed change from
        yesterday's close. That's fragile -- it silently produces a wrong-but-plausible previous
        close whenever a quote's `net_change` doesn't behave exactly that way (e.g. right around
        a gap-open), which showed up as a change badge reading the wrong *direction* entirely
        (e.g. "+0.5%" on a day that gapped down). The only way to get the real previous close
        without trusting a derived field is to ask for it directly -- the daily candle endpoint's
        most recent completed session (`to_date` = yesterday) *is* that close, full stop.

        Cached per (instrument_key, day) since this value is fixed for the entire trading day --
        avoids re-fetching it on every position-quotes/bootstrap poll.
        """
        cache_key = ("previous_close", instrument_key, today.isoformat())
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached["close"]

        yesterday = today - timedelta(days=1)
        from_date = (today - timedelta(days=10)).isoformat()
        try:
            payload = await self.upstox.get_historical_candle(
                access_token,
                instrument_key,
                unit="days",
                interval="1",
                to_date=yesterday.isoformat(),
                from_date=from_date,
            )
        except UpstoxApiError:
            return 0.0

        close = _latest_daily_close(payload)
        _cache_set(cache_key, {"close": close}, ttl_seconds=3600.0)
        return close


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
    # average_price is the currently-open lot's real average entry -- buy_price/sell_price are
    # Upstox's *cumulative day-total* buy/sell averages for the instrument, not the open lot's
    # entry, so they go stale/wrong the moment an instrument has more than one round trip in a
    # day (e.g. bought, squared off, bought again -- buy_price then blends both fills). Don't use
    # them as a stand-in.
    #
    # average_price can still legitimately read 0 for a moment right after a fresh fill, before
    # Upstox's own position-keeping catches up -- that gap is handled client-side (see
    # MainViewModel.mergeOpenPositions), which keeps the previous confirmed entry price until a
    # real one arrives, rather than papering over it here with a value that can be permanently
    # wrong.
    value = position.get("average_price")
    return float(value) if isinstance(value, (int, float)) else 0.0


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


def _option_side(side: Any) -> Optional[dict[str, Any]]:
    """Reshapes one `call_options`/`put_options` entry from Upstox's `/option/chain` response
    into a flat dict -- None if this strike simply has no listed contract for that side (Upstox
    omits the key entirely for those, same as the old `/option/contract`-based behavior).
    """
    if not isinstance(side, dict):
        return None
    market_data = side.get("market_data")
    market_data = market_data if isinstance(market_data, dict) else {}
    greeks = side.get("option_greeks")
    greeks = greeks if isinstance(greeks, dict) else {}
    return {
        "instrument_key": _string_value(side, "instrument_key"),
        "ltp": _number_value(market_data, "ltp"),
        "bid_price": _number_value(market_data, "bid_price"),
        "ask_price": _number_value(market_data, "ask_price"),
        "bid_qty": _number_value(market_data, "bid_qty"),
        "ask_qty": _number_value(market_data, "ask_qty"),
        "oi": _number_value(market_data, "oi"),
        "prev_oi": _number_value(market_data, "prev_oi"),
        "volume": _number_value(market_data, "volume"),
        "delta": _number_value(greeks, "delta"),
        "gamma": _number_value(greeks, "gamma"),
        "theta": _number_value(greeks, "theta"),
        "vega": _number_value(greeks, "vega"),
        "iv": _number_value(greeks, "iv"),
    }


def _last_price(quote: dict[str, Any]) -> float:
    return _number_value(quote, "last_price", "ltp")


def _latest_daily_close(payload: dict[str, Any]) -> float:
    """The most recent completed session's closing price from a `get_historical_candle(unit=
    "days", ...)` response -- `[timestamp, open, high, low, close, volume, oi]` rows, picked by
    max timestamp rather than trusting Upstox's own row ordering (not documented as guaranteed).
    `0.0` if the response has no usable rows (e.g. a brand-new instrument with no prior session).
    """
    data = payload.get("data")
    rows = data.get("candles") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return 0.0
    usable = [row for row in rows if isinstance(row, list) and len(row) >= 5]
    if not usable:
        return 0.0
    latest = max(usable, key=lambda row: str(row[0]))
    try:
        return float(latest[4])
    except (TypeError, ValueError):
        return 0.0


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

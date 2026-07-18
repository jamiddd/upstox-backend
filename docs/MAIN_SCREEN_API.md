# Main Screen API

Backend contract for the app's option trading main screen.

All endpoints require:

```text
X-API-Key: <MOBILE_API_KEY>
```

## Bootstrap

```http
GET /api/main/bootstrap
GET /api/main/bootstrap?underlying_key=NSE_INDEX%7CNifty%2050&expiry_date=2026-07-16
```

Defaults:

```text
underlying_key=NSE_INDEX|Nifty 50
expiry_date=<nearest available expiry>
```

Returns the underlying, available expiries, account summary, and currently open positions.
`previous_close` is the underlying's last trading day's closing price (from the same full-quote
call already made for `spot_price`), letting the app show a "(+0.40%)" change badge next to the
spot price without a separate history/OHLC call.

FIX: this is derived as `last_price - net_change`, not `ohlc.close`. Upstox documents `ohlc.close`
as "the most recent closing price of the symbol", but in practice it tracks the *current,
still-forming* session's close and converges to `last_price` while that session is live/open --
using it made every "(+x.xx%)" badge in the app read ~0% for anything actively trading, only
becoming meaningful once a symbol's session had fully ended for the day. `net_change` is
separately documented as "the absolute change from yesterday's close to last traded price", which
gives the real previous close directly regardless of whether today's session has closed yet.

```json
{
  "underlying": {
    "instrument_key": "NSE_INDEX|Nifty 50",
    "symbol": "NIFTY",
    "name": "NIFTY",
    "spot_price": 25050.0,
    "previous_close": 24950.0
  },
  "expiries": ["2026-07-16", "2026-07-23"],
  "selected_expiry": "2026-07-16",
  "summary": {
    "opening_balance": 100000.0,
    "profit_loss": 400.0,
    "closing_balance": 102300.0,
    "available_margin": 99980.0,
    "margin_used": 10000.0,
    "payin_amount": 1900.0
  },
  "open_positions": [
    {
      "instrument_key": "NSE_FO|111",
      "trading_symbol": "NIFTY26JUL25000CE",
      "quantity": 75.0,
      "entry_price": 120.0,
      "last_price": 125.0,
      "pnl": 375.0
    }
  ]
}
```

## Selected Strike Quote

```http
GET /api/main/selected-quote?expiry_date=2026-07-16&strike_price=25000&option_type=CE
```

Optional:

```text
underlying_key=NSE_INDEX|Nifty 50
```

The app sends the selected strike and CE/PE toggle. The backend resolves the matching option contract and returns only the values needed for the spot, buy, and sell text views.

```json
{
  "underlying": {
    "instrument_key": "NSE_INDEX|Nifty 50",
    "spot_price": 25050.0
  },
  "contract": {
    "instrument_key": "NSE_FO|111",
    "trading_symbol": "NIFTY26JUL25000CE",
    "strike_price": 25000.0,
    "option_type": "CE",
    "lot_size": 65.0,
    "freeze_quantity": 1755.0,
    "tick_size": 0.05,
    "ltp": 125.0,
    "bid_price": 124.5,
    "ask_price": 125.5
  }
}
```

## Option Chain

```http
GET /api/main/option-chain?expiry_date=2026-07-16&underlying_key=NSE_INDEX|Nifty 50
```

Returns every listed strike's **live** CE/PE market data and option greeks for the given
underlying + expiry -- powers the app's smart strike selector (ATM/delta-target/liquidity/
manual-offset/DTE-aware modes all pick from this same per-strike data, client-side; see
`ui/main/strikeselection/StrikeSelector.kt` in the app repo).

FIX: this used to wrap Upstox's `/option/contract` endpoint, which only returns bare contract
metadata (instrument key, lot size, tick size) -- no LTP, no bid/ask, no OI, no greeks, so there
was no data to build anything "smart" from. Now wraps Upstox's `/option/chain` endpoint instead,
which returns everything needed for every strike in one call. Cached much more briefly than
before (15s, not 600s) since this data is live-changing throughout the day, not static.

```json
{
  "underlying_key": "NSE_INDEX|Nifty 50",
  "expiry_date": "2026-07-16",
  "underlying_spot_price": 25050.0,
  "strikes": [
    {
      "strike_price": 25000.0,
      "ce": {
        "instrument_key": "NSE_FO|111",
        "ltp": 125.0,
        "bid_price": 124.5,
        "ask_price": 125.5,
        "bid_qty": 300.0,
        "ask_qty": 450.0,
        "oi": 1250000.0,
        "prev_oi": 1180000.0,
        "volume": 5400000.0,
        "delta": 0.52,
        "gamma": 0.0012,
        "theta": -18.4,
        "vega": 12.1,
        "iv": 14.2
      },
      "pe": {
        "instrument_key": "NSE_FO|222",
        "ltp": 90.0,
        "bid_price": 89.5,
        "ask_price": 90.5,
        "bid_qty": 200.0,
        "ask_qty": 350.0,
        "oi": 980000.0,
        "prev_oi": 1020000.0,
        "volume": 4100000.0,
        "delta": -0.47,
        "gamma": 0.0012,
        "theta": -17.9,
        "vega": 12.0,
        "iv": 13.9
      }
    }
  ]
}
```

A strike missing a listed CE or PE contract simply omits that key (e.g. deep ITM/OTM strikes
sometimes only have one side listed).

## Position Quotes

```http
GET /api/main/position-quotes?instrument_keys=NSE_FO%7C111,NSE_FO%7C222
```

Returns compact LTP + previous-close snapshots for any instrument keys -- originally for open
positions, but it's a generic quote call, also used to poll the toolbar's watchlist ticker
(regular NSE/BSE instruments AND Upstox's Global Instruments, e.g. `GLOBAL_INDEX|^GSPC` for S&P
500, `GLOBAL_INDEX|SGX NIFTY` for GIFT NIFTY, `GLOBAL_INDICATOR|USDINR` -- see Upstox's Global
Instruments file; the underlying Full Market Quote call supports these directly). `previous_close`
lets the app color each entry by direction (up/down vs. yesterday's close).

```json
{
  "positions": [
    {
      "instrument_key": "NSE_FO|111",
      "ltp": 125.0,
      "previous_close": 120.0
    }
  ]
}
```

## Summary

```http
GET /api/main/summary
```

```json
{
  "opening_balance": 100000.0,
  "profit_loss": 400.0,
  "closing_balance": 102300.0,
  "available_margin": 99980.0,
  "margin_used": 10000.0,
  "payin_amount": 1900.0
}
```

All six fields' source paths are confirmed against a real `GET /v3/user/get-funds-and-margin` response (see "Raw Funds and Margin" below):

- `opening_balance` (`available_to_trade.cash_available_to_trade.cash.opening_balance`) is a genuine static start-of-day snapshot -- it does NOT move when cash is added/withdrawn intraday.
- `profit_loss` sums *every* position Upstox returns for the day, including ones already squared off (quantity 0) -- not just currently-open ones -- since a closed position still carries its realized P&L here; summing only open positions would read as 0 on a day that was all open-and-close scalps with nothing left open.
- `available_margin` (`available_to_trade.total`) is the actual "can I place another order right now" number: cash + pledge margin, already net of margin blocked by open positions and today's cash movement.
- `margin_used` sums `cash_available_to_trade.margin_used.total` and `pledge_available_to_trade.margin_used.total` -- margin currently locked by open positions.
- `payin_amount` is `cash.added_today + cash.withdrawn_today` (the latter already negative) -- net cash movement today.
- `closing_balance` = `opening_balance + payin_amount + profit_loss` -- includes today's net cash movement (not just `opening_balance + profit_loss` as before), since a mid-day deposit is real money added to the account, not "profit".

## Underlying Signals

```http
GET /api/main/underlying-signals
GET /api/main/underlying-signals?underlying_key=NSE_INDEX%7CNifty%2050
```

Glanceable technical-analysis tags for the underlying -- shown to the app's user just before they
place a strike order, so they can see e.g. "is the underlying above its 9 EMA" without leaving the
trading screen. Deliberately computed on the **underlying's** own price action (spot/futures),
not the option contract about to be traded -- an option premium is dominated by theta decay and
IV changes rather than the underlying's own trend/momentum, so an EMA/ATR/opening-range reading
on the premium itself would be meaningless here.

```json
{
  "ltp": 25050.0,
  "ema9_5m": {"value": 25010.5, "position": "above"},
  "ema9_15m": {"value": 24990.0, "position": "above"},
  "atr14_5m": 42.3,
  "opening_range": {"window_minutes": 15, "high": 25080.0, "low": 24990.0, "position": "inside"},
  "previous_day": {"high": 25100.0, "low": 24900.0, "close": 24980.0},
  "pivots": {"p": 24993.3, "r1": 25086.6, "r2": 25193.3, "s1": 24886.6, "s2": 24793.3},
  "round_step": 50.0,
  "nearest_level": {"label": "R1 Pivot", "value": 25086.6, "distance_percent": 0.15},
  "tags": ["Above 5m EMA9", "Above 15m EMA9", "Inside opening range", "Near R1 Pivot"]
}
```

- `ema9_5m` / `ema9_15m`: 9-period EMA of closes on 5-minute and 15-minute candles respectively --
  the 5m read is meant for scalping timing, the 15m read for the broader intraday bias. `position`
  is `"above"`/`"below"`/`"at"` LTP relative to the EMA value; either can be `null` (with `value`
  also `null`) if there isn't yet enough candle history to compute it.
- `atr14_5m`: 14-period Average True Range (Wilder's smoothing) on the 5-minute series -- a
  volatility gauge, in underlying price units (e.g. NIFTY points).
- `opening_range`: the high/low of the first 15 minutes of today's session (9:15-9:30 IST, the
  first three 5-minute candles), and where LTP currently sits relative to that range
  (`"above"`/`"below"`/`"inside"`).
- `previous_day`: the prior completed trading session's high/low/close, straight off the daily
  candle series.
- `pivots`: classic pivot points (`p`/`r1`/`r2`/`s1`/`s2`) computed from `previous_day`'s
  high/low/close.
- `round_step`: the underlying's own strike spacing (the most common gap between consecutive
  option-chain strikes for this underlying), used to find the two round psychological numbers
  bracketing LTP. `0.0` if there isn't enough strike data to derive a step.
- `nearest_level`: whichever of `previous_day`'s three values, the five pivot levels, or the two
  round numbers is closest to LTP, **only if** it's within 0.15% of LTP -- `null` if nothing is
  that close right now.
- `tags`: a small set of ready-to-render short labels (e.g. `"Above 5m EMA9"`, `"ATR 42.3"`,
  `"Near R1 Pivot"`) built from the fields above -- the client can display these directly without
  any string-building of its own.

Candle-derived values (the EMAs, ATR, opening range, previous-day/pivots, round step) are cached
~60 seconds -- they only meaningfully change when a new candle closes, not on every feed tick.
`ltp` and everything computed relative to it (`position` fields, `nearest_level`) are read fresh
on every call.

## Raw Funds and Margin

```http
GET /api/user/get-funds-and-margin
```

Returns the complete Upstox V3 `/user/get-funds-and-margin` response without reshaping it. Use
`/api/main/summary` when the screen only needs the normalized summary fields.

## Refresh Cadence

The backend keeps short in-memory caches to avoid excessive Upstox calls:

```text
selected/position quote data: ~0.75 seconds
positions: ~1 second
summary: ~5 seconds
option contracts/expiries: ~10 minutes
underlying-signals candle-derived values (EMAs/ATR/opening range/pivots): ~60 seconds
```

For millisecond-level flashing values, the next backend step should be a market data WebSocket bridge or authorization endpoint.

## Live Market Feed

For the lowest-latency bid/ask, spot, and position LTP updates, use Upstox Market Data Feed V3 instead of REST polling.

First request a one-time WebSocket URL:

```http
GET /api/market/feed/authorize
```

Response:

```json
{
  "status": "success",
  "data": {
    "authorized_redirect_uri": "wss://..."
  }
}
```

The app should connect directly to `authorized_redirect_uri`. The URL is single-use, so request a new one for every WebSocket connection attempt.

Subscribe with Upstox's binary/protobuf V3 request format. For this screen:

```json
{
  "guid": "<client-generated-id>",
  "method": "sub",
  "data": {
    "mode": "full",
    "instrumentKeys": [
      "NSE_INDEX|Nifty 50",
      "NSE_FO|<selected_contract_key>"
    ]
  }
}
```

Use `full` for the selected option contract because the buy/sell buttons need best bid/ask. The selected contract feed includes:

```text
fullFeed.marketFF.ltpc.ltp
fullFeed.marketFF.marketLevel.bidAskQuote[0].bidP
fullFeed.marketFF.marketLevel.bidAskQuote[0].askP
```

For open positions, subscribe to their instrument keys. If only LTP is needed for local P&L, `ltpc` mode is enough:

```json
{
  "guid": "<client-generated-id>",
  "method": "sub",
  "data": {
    "mode": "ltpc",
    "instrumentKeys": [
      "NSE_FO|<position_1>",
      "NSE_FO|<position_2>"
    ]
  }
}
```

When the selected strike changes, unsubscribe the previous selected contract and subscribe the new contract in `full` mode. When positions open or close, update the `ltpc` subscriptions accordingly.

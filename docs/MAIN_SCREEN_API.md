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

```json
{
  "underlying": {
    "instrument_key": "NSE_INDEX|Nifty 50",
    "symbol": "NIFTY",
    "name": "NIFTY",
    "spot_price": 25050.0
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

Returns every listed strike's CE/PE contract metadata for the given underlying + expiry, so the
app can determine the real strike interval and the at-the-money strike from the actual listed
strikes instead of guessing a step size (it varies by underlying: 50 for NIFTY, 100 for
BANKNIFTY, arbitrary for stocks). This does not include live bid/ask prices -- call
`selected-quote` for the one strike the app resolves as ATM to get its live price.

```json
{
  "underlying_key": "NSE_INDEX|Nifty 50",
  "expiry_date": "2026-07-16",
  "strikes": [
    {
      "strike_price": 25000.0,
      "ce": {
        "instrument_key": "NSE_FO|111",
        "trading_symbol": "NIFTY26JUL25000CE",
        "lot_size": 65.0,
        "freeze_quantity": 1755.0,
        "tick_size": 0.05
      },
      "pe": {
        "instrument_key": "NSE_FO|222",
        "trading_symbol": "NIFTY26JUL25000PE",
        "lot_size": 65.0,
        "freeze_quantity": 1755.0,
        "tick_size": 0.05
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

Returns compact LTP snapshots for open positions so the app can update local P&L.

```json
{
  "positions": [
    {
      "instrument_key": "NSE_FO|111",
      "ltp": 125.0
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

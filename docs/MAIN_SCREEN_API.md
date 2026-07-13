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
    "profit_loss": 375.0,
    "closing_balance": 100375.0
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
    "ltp": 125.0,
    "bid_price": 124.5,
    "ask_price": 125.5
  }
}
```

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
  "profit_loss": 375.0,
  "closing_balance": 100375.0
}
```

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

# Order Placement API

Backend contract for placing app smart orders.

All endpoints require:

```text
X-API-Key: <MOBILE_API_KEY>
```

## Smart Bracket Order

```http
POST /api/orders/smart-bracket
```

This endpoint places a bracket-like order using Upstox multi-leg GTT. It does not calculate trading levels. The client must send the selected entry, target, and stop-loss prices.

Request:

```json
{
  "instrument_key": "NSE_FO|111",
  "transaction_type": "BUY",
  "quantity": 75,
  "product": "I",
  "entry_trigger_type": "IMMEDIATE",
  "entry_trigger_price": 125.5,
  "target_trigger_price": 140.0,
  "stoploss_trigger_price": 118.0,
  "slice_quantity": 1800,
  "market_protection": -1
}
```

Fields:

```text
instrument_key required
transaction_type required, BUY|SELL
quantity required, positive integer
product optional, I|D|MTF, default I
entry_trigger_type optional, ABOVE|BELOW|IMMEDIATE, default IMMEDIATE
entry_trigger_price required, positive number
target_trigger_price required, positive number
stoploss_trigger_price required, positive number
trailing_gap optional, positive number
market_protection optional, -1 to 25
slice_quantity optional, positive integer, defaults to DEFAULT_ORDER_SLICE_QUANTITY
```

The backend slices `quantity` into multiple Upstox GTT orders when it exceeds `slice_quantity`. This keeps freeze-quantity handling out of the client. Configure the server default with:

```env
DEFAULT_ORDER_SLICE_QUANTITY=1800
```

For `quantity=3750` and `slice_quantity=1800`, the backend submits three GTT orders:

```text
1800
1800
150
```

Upstox payload submitted by the backend for each slice:

```json
{
  "type": "MULTIPLE",
  "quantity": 75,
  "product": "I",
  "rules": [
    {
      "strategy": "ENTRY",
      "trigger_type": "IMMEDIATE",
      "trigger_price": 125.5,
      "market_protection": -1
    },
    {
      "strategy": "TARGET",
      "trigger_type": "IMMEDIATE",
      "trigger_price": 140.0,
      "market_protection": -1
    },
    {
      "strategy": "STOPLOSS",
      "trigger_type": "IMMEDIATE",
      "trigger_price": 118.0,
      "market_protection": -1
    }
  ],
  "instrument_token": "NSE_FO|111",
  "transaction_type": "BUY"
}
```

Response:

```json
{
  "status": "success",
  "source": "upstox_gtt",
  "total_quantity": 3750,
  "slice_quantity": 1800,
  "slice_count": 3,
  "slices": [
    {
      "slice_number": 1,
      "quantity": 1800,
      "submitted_order": {},
      "upstox_response": {
        "status": "success",
        "data": {
          "gtt_order_ids": ["GTT-123"]
        }
      }
    }
  ]
}
```

Notes:

```text
This is not a classic exchange/broker bracket order.
It uses Upstox GTT MULTIPLE with ENTRY, TARGET, and STOPLOSS rules.
For BUY entry, Upstox treats TARGET/STOPLOSS as SELL exits; for SELL entry, exits are BUY.
TARGET and STOPLOSS trigger_type are always IMMEDIATE as required by Upstox.
```

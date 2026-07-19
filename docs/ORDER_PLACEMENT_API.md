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
slice_quantity optional, positive integer
```

The backend validates the selected instrument against Upstox's BOD instrument master before placing the order:

```text
quantity must be a multiple of lot_size
entry_trigger_price must align to tick_size
target_trigger_price must align to tick_size
stoploss_trigger_price must align to tick_size
```

The backend also slices `quantity` into multiple Upstox GTT orders when it exceeds the instrument `freeze_quantity`. This keeps freeze-quantity handling out of the client. If `slice_quantity` is provided, it overrides the instrument freeze quantity.

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
Normal Upstox v3 place-order supports slice=true, but GTT place order does not document slice=true, so the backend handles slicing for smart bracket orders.
```

## List GTT Orders

```http
GET /api/orders/gtt?instrument_key=NSE_FO|111
```

Returns the active (not `CANCELLED`/`REJECTED`/`COMPLETED`) GTT orders for one instrument --
lets the client find the bracket order behind an open position so its target/stoploss can be
shown and edited. `instrument_key` is required.

Response (raw passthrough of the matching Upstox GTT order entries):

```json
[
  {
    "gtt_order_id": "GTT-111",
    "instrument_token": "NSE_FO|111",
    "quantity": 75,
    "product": "I",
    "status": "ACTIVE",
    "rules": [
      { "strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": 125.5 },
      { "strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": 140.0 },
      { "strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": 118.0 }
    ]
  }
]
```

## Attach GTT Exits

```http
POST /api/orders/gtt/attach-exits
```

Attaches a target and a stoploss to an already-open position that has **no existing GTT
bracket** (`GET /api/orders/gtt` above returned nothing usable). Unlike Smart Bracket Order,
this never submits a new entry: it places two independent Upstox GTT `type=SINGLE` orders
(one rule each -- `TARGET` and `STOPLOSS`), so it cannot accidentally re-enter and double the
position the way reusing the `MULTIPLE`+`IMMEDIATE`-entry shape would.

`exit_transaction_type` is the *exit* side, i.e. the opposite of how the position was opened
(`"SELL"` to attach exits to a long position, `"BUY"` for a short) -- required because a
`SINGLE`-type order has no `ENTRY` leg to infer direction from.

Request:

```json
{
  "instrument_key": "NSE_FO|111",
  "quantity": 75,
  "product": "I",
  "exit_transaction_type": "SELL",
  "target_trigger_price": 140.0,
  "stoploss_trigger_price": 115.0
}
```

Fields:

```text
instrument_key required
quantity required, positive integer, validated against the instrument's lot_size
product optional, I|D|MTF, default I
exit_transaction_type required, BUY|SELL
target_trigger_price required, positive number, validated against tick_size
stoploss_trigger_price required, positive number, validated against tick_size
```

The two legs are placed independently, so one failing doesn't stop the other. Response:

```json
{
  "status": "partial_success",
  "target": {
    "status": "success",
    "submitted_order": {},
    "upstox_response": {}
  },
  "stoploss": {
    "status": "error",
    "submitted_order": {},
    "error": "GTT order cannot be placed"
  }
}
```

Top-level `status` is `success` (both legs placed), `partial_success` (one placed), or `error`
(neither placed).

## Modify GTT Order

```http
PUT /api/orders/gtt/modify
```

Re-points an existing GTT bracket's target/stoploss trigger prices (e.g. after the client fetched
its current rules from `GET /api/orders/gtt` above). The entry fields must be resent unchanged --
Upstox's GTT modify contract expects the full rule set, not a partial patch.

Request:

```json
{
  "gtt_order_id": "GTT-111",
  "instrument_key": "NSE_FO|111",
  "quantity": 75,
  "product": "I",
  "entry_trigger_type": "IMMEDIATE",
  "entry_trigger_price": 125.5,
  "target_trigger_price": 145.0,
  "stoploss_trigger_price": 115.0
}
```

Fields:

```text
gtt_order_id required
instrument_key required -- used to validate target_trigger_price/stoploss_trigger_price against the instrument's tick_size, same as Smart Bracket Order above
quantity required, positive integer
product optional, I|D|MTF, default I
entry_trigger_type optional, ABOVE|BELOW|IMMEDIATE, default IMMEDIATE
entry_trigger_price required, positive number
target_trigger_price required, positive number
stoploss_trigger_price required, positive number
trailing_gap optional, positive number
```

Response: the raw Upstox GTT modify response.

## Modify Orders

```http
PUT /api/orders/modify
```

This endpoint modifies one or more regular open/pending orders through Upstox V3. The
backend does not impose an order-count limit: it submits each modification separately
and continues after individual Upstox rejections.

Request:

```json
{
  "orders": [
    {
      "order_id": "240108010918222",
      "validity": "DAY",
      "price": 126.5,
      "order_type": "LIMIT",
      "trigger_price": 0,
      "quantity": 75,
      "disclosed_quantity": 0
    }
  ]
}
```

`order_id`, `validity`, `price`, `order_type`, and `trigger_price` are required for
each item. `quantity`, `disclosed_quantity`, and `market_protection` are optional.

Response:

```json
{
  "status": "partial_success",
  "summary": {
    "total": 2,
    "success": 1,
    "failed": 1
  },
  "orders": [
    {
      "order_id": "240108010918222",
      "status": "success",
      "upstox_response": {}
    },
    {
      "order_id": "240108010918223",
      "status": "error",
      "error": {
        "message": "Order cannot be modified",
        "upstox_code": "UDAPI100041"
      }
    }
  ]
}
```

The top-level status is `success`, `partial_success`, or `error`. A failed item does
not roll back successful modifications because Upstox processes them as independent
orders.

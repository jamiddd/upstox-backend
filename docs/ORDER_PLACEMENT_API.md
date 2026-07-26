# Order Placement API

Backend contract for placing app smart orders.

The app used to also have `POST /api/orders/market-bracket` (a real immediate-fill MARKET entry
with target/stoploss GTT exits attached after) plus `GET /api/orders/pending-exits` and
`PUT /api/orders/pending-exits/target-price` for viewing/editing those exits before they resolved
-- retired for being unreliable in live trading. A later fallback, `POST
/api/orders/gtt/attach-exits` (for attaching protection to a position with no GTT bracket at
all, e.g. opened outside the app) plus its own `POST /api/orders/cancel-resting-exit` helper, was
retired too -- its watched-target mechanism (a background poller that armed the target as a
price level and fired a MARKET order once price crossed it) had a real silent-failure mode if
that market order itself failed after the position was already dropped from tracking. Every
order the app places now goes exclusively through Smart Bracket Order below, a real Upstox GTT
bracket for both target and stoploss placed atomically at entry -- there is no in-app recovery
path for a position that somehow ends up without one; that has to be handled directly in
Upstox's own app.

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
GET /api/orders/gtt?instrument_key=NSE_FO|111&include_history=true
GET /api/orders/gtt
```

By default, returns active (not `CANCELLED`/`REJECTED`/`COMPLETED`) GTT orders. Pass
`instrument_key` to restrict the result to one instrument (used for position bracket editing);
omit it to populate the Main screen's separate GTT Open Orders section.

Upstox reports lifecycle status on each `rules[].status` and can omit a top-level order status.
The backend normalizes those rule states into a top-level `status` and filters terminal brackets;
for example, an ENTRY rule marked `CANCELLED` with inactive TARGET/STOPLOSS siblings is excluded,
while any `SCHEDULED`/`OPEN`/`PENDING` rule keeps the bracket active.

With `include_history=true`, also returns `COMPLETED` brackets (still excludes
`CANCELLED`/`REJECTED`, which never actually fired) -- lets the client recover the
target/stoploss a now-closed position had, by matching a specific order's own fill timestamp
against each returned GTT's `created_at` (Unix microseconds) and picking the closest one that
isn't after it.

Response (raw passthrough of the matching Upstox GTT order entries):

```json
[
  {
    "gtt_order_id": "GTT-111",
    "instrument_token": "NSE_FO|111",
    "quantity": 75,
    "product": "I",
    "status": "ACTIVE",
    "created_at": 1740641185000000,
    "rules": [
      { "strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": 125.5 },
      { "strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": 140.0 },
      { "strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": 118.0 }
    ]
  }
]
```

## Modify GTT Order

```http
PUT /api/orders/gtt/modify
```

Updates an existing GTT bracket's entry, quantity, target, and stoploss values. Upstox's GTT
modify contract expects the full rule set, not a partial patch, so all rules are resent together.
The backend validates the edited quantity and every edited price against the instrument rules.

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

## Cancel GTT Order

```text
DELETE /api/orders/gtt/cancel
```

Cancels one untriggered GTT order and all of its remaining rules. The Android client asks for
confirmation before calling this endpoint.

```json
{
  "gtt_order_id": "GTT-111"
}
```

Response: the raw Upstox GTT cancel response.

## Exit Positions

```http
POST /api/orders/exit-positions
```

Flattens open positions with an immediate market order each (opposite side of the position).
Optionally scoped to a subset via `instrument_keys` -- e.g. the app's "close only positive/
negative positions" menu computes the matching instrument keys client-side (it already has live
P&L from the WebSocket feed) and sends just those.

Request:

```json
{
  "instrument_keys": ["NSE_FO|111"]
}
```

`instrument_keys` is optional; omitting it (or sending `null`) closes every open position --
identical to `POST /api/orders/exit-all` (unchanged, still used by the max-loss auto square-off).

Response:

```json
{
  "status": "success",
  "positions_found": 1,
  "results": [
    {
      "instrument_key": "NSE_FO|111",
      "transaction_type": "SELL",
      "quantity": 75,
      "status": "success",
      "attempts": 1,
      "upstox_response": {}
    }
  ]
}
```

Each position is closed independently -- one failing doesn't stop the others; check each result's
own `status`. A position's own flattening order is internally sliced against its instrument's
`freeze_quantity` (same mechanism as Smart Bracket Order/Attach GTT Exits).

An exit that returns an error is retried up to three times. Before each retry the backend waits,
re-fetches broker positions, retries any tracked resting-stop cancellation, and submits only the
position's remaining live quantity. An accepted first attempt is never blindly duplicated.
`attempts` reports how many submissions were needed; after the third failed attempt the result
remains `"error"` for manual intervention.

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

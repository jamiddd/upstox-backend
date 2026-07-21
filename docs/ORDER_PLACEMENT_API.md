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

## Market Bracket Order

```http
POST /api/orders/market-bracket
```

Places a real immediate-fill MARKET order for entry, then attaches target/stoploss GTT exits
(same mechanism as Attach GTT Exits below) once entry is submitted. Use this instead of Smart
Bracket Order when the entry must actually fill at market -- **a Smart Bracket Order's GTT ENTRY
leg is always executed by Upstox as a LIMIT order at `entry_trigger_price`, even with
`entry_trigger_type=IMMEDIATE`** (a GTT order is always placed as a LIMIT order on execution, per
Upstox's own docs), so it can silently behave like a limit order when the caller actually wanted
a market fill.

Request:

```json
{
  "instrument_key": "NSE_FO|111",
  "transaction_type": "BUY",
  "quantity": 75,
  "product": "I",
  "target_trigger_price": 140.0,
  "stoploss_trigger_price": 118.0
}
```

Fields:

```text
instrument_key required
transaction_type required, BUY|SELL
quantity required, positive integer
product optional, I|D|MTF, default I
target_trigger_price required, positive number
stoploss_trigger_price required, positive number
slice_quantity optional, positive integer
```

No `entry_trigger_price`/`entry_trigger_type` -- there's nothing to submit for a real market
entry, it fills at whatever the market gives. Same tick-size/lot-size validation and
freeze-quantity slicing as Smart Bracket Order applies to `quantity`/`target_trigger_price`/
`stoploss_trigger_price`.

Only the quantity that was actually submitted successfully as a market entry gets exits attached
-- a slice that fails to enter does not end up with a stray target/stoploss bracket for shares
that were never bought.

Response:

```json
{
  "status": "success",
  "source": "upstox_market_with_gtt_exits",
  "total_quantity": 75,
  "entered_quantity": 75,
  "slice_quantity": 75,
  "slice_count": 1,
  "entry_slices": [
    {
      "slice_number": 1,
      "quantity": 75,
      "status": "success",
      "upstox_response": { "status": "success", "data": { "order_ids": ["..."] } }
    }
  ],
  "exits": {
    "status": "success",
    "total_quantity": 75,
    "slice_quantity": 75,
    "slice_count": 1,
    "slices": [
      {
        "slice_number": 1,
        "quantity": 75,
        "target": { "status": "success", "submitted_order": {}, "upstox_response": {} },
        "stoploss": { "status": "success", "submitted_order": {}, "upstox_response": {} }
      }
    ]
  }
}
```

`status` is `"success"` (every entry slice and every exit leg placed), `"partial_success"` (some
entered but not all, or entry succeeded but an exit leg failed), or `"error"` (nothing entered --
`exits` is `null` in that case, since there's no position to attach exits to).

## List GTT Orders

```http
GET /api/orders/gtt?instrument_key=NSE_FO|111
GET /api/orders/gtt?instrument_key=NSE_FO|111&include_history=true
```

By default, returns the active (not `CANCELLED`/`REJECTED`/`COMPLETED`) GTT orders for one
instrument -- lets the client find the bracket order behind an open position so its
target/stoploss can be shown and edited. `instrument_key` is required.

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

## Attach GTT Exits

```http
POST /api/orders/gtt/attach-exits
```

Attaches a target and a stoploss to an already-open position that has **no existing GTT
bracket** (`GET /api/orders/gtt` above returned nothing usable). Unlike Smart Bracket Order,
this never submits a new entry.

Only the **stoploss** is ever placed as a real Upstox order (a plain `SL-M` order, not GTT --
GTT requires exactly one `ENTRY` rule in every order, so a `type=SINGLE` order with only a
`TARGET` or `STOPLOSS` rule is rejected outright: "One ENTRY strategy is required."). Placing
*both* legs as live orders doesn't work either: Upstox reserves the full held quantity against
the first live SELL order placed, so a second SELL order for the same quantity has nothing left
to "cover" it and gets margin-checked as a brand new naked short (a "You need to add Rs. X in
your account" rejection) -- there's no way to have two live sell orders each covering the full
position at once.

So the **target** is armed as a price level the backend's own background watcher
(`app/services/oco_watcher.py`) polls against live quotes every 5s, and only becomes a real
`MARKET` order once price actually crosses it -- at which point the stoploss order above is
cancelled. This trades the target's precision (it fires as a market order once the watcher
notices the cross, not a resting limit order at the exact price) for correctness (no rejected
second order). The response below still carries a `target` sub-object per slice (mirroring
`stoploss`) purely so existing per-leg error handling keeps working -- there's no separate
placement outcome for `target` any more since it was never a live order.

`exit_transaction_type` is the *exit* side, i.e. the opposite of how the position was opened
(`"SELL"` to attach exits to a long position, `"BUY"` for a short) -- required since a plain
order has no `ENTRY` leg to infer direction from.

Like Smart Bracket Order, `quantity` is sliced into multiple stoploss orders (each with its own
watched target) when it exceeds the instrument's `freeze_quantity` -- a position sized over
freeze quantity would otherwise be rejected outright by Upstox. If `slice_quantity` is provided,
it overrides the instrument freeze quantity.

To edit a pending exit later, `GET /api/orders/pending-exits?instrument_key=...` returns the
still-live ones (stoploss trigger price read from the live order, target trigger price read back
from the backend's own store); `PUT /api/orders/pending-exits/target-price` re-points the stored
target (no Upstox call), and `PUT /api/orders/modify` (see below) re-points the stoploss order
like any other regular order.

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
slice_quantity optional, positive integer
```

Each slice's target/stoploss legs are placed independently, so one failing doesn't stop the
rest. Response:

```json
{
  "status": "partial_success",
  "total_quantity": 3750,
  "slice_quantity": 1800,
  "slice_count": 3,
  "slices": [
    {
      "slice_number": 1,
      "quantity": 1800,
      "target": { "status": "success", "submitted_order": {}, "upstox_response": {} },
      "stoploss": { "status": "success", "submitted_order": {}, "upstox_response": {} }
    },
    {
      "slice_number": 2,
      "quantity": 1800,
      "target": { "status": "success", "submitted_order": {}, "upstox_response": {} },
      "stoploss": {
        "status": "error",
        "submitted_order": {},
        "error": "GTT order cannot be placed"
      }
    },
    {
      "slice_number": 3,
      "quantity": 150,
      "target": { "status": "success", "submitted_order": {}, "upstox_response": {} },
      "stoploss": { "status": "success", "submitted_order": {}, "upstox_response": {} }
    }
  ]
}
```

Top-level `status` is `success` (every slice's stoploss placed), `partial_success` (some placed),
or `error` (none placed).

## Get Pending Exits

```http
GET /api/orders/pending-exits?instrument_key=NSE_FO|111
```

The plain-order counterpart of `GET /api/orders/gtt` above -- pending exits (see Attach GTT
Exits) still awaiting reconciliation for one instrument, so the client can find and edit the exit
behind a position that has no GTT bracket instead of only ever finding nothing and attaching a
redundant second one. `target_trigger_price` is read from the backend's own store (never a live
order); `stoploss_trigger_price`/`quantity`/`product` are read from the live stoploss order. Only
returns an entry whose stoploss is still confirmed live -- one that's already terminal
(filled/cancelled/rejected) is about to be dropped by `oco_watcher`'s own next tick.

Response:

```json
[
  {
    "stoploss_order_id": "260721000423257",
    "quantity": 75,
    "product": "I",
    "target_trigger_price": 140.0,
    "stoploss_trigger_price": 115.0
  }
]
```

## Update Pending Exit Target Price

```http
PUT /api/orders/pending-exits/target-price
```

Re-points a pending exit's stored target price. Doesn't touch Upstox at all -- the target is
never a live order (see Attach GTT Exits above). To change the stoploss price instead, use the
ordinary `PUT /orders/modify` below against `stoploss_order_id` (a real order).

Request:

```json
{
  "instrument_key": "NSE_FO|111",
  "stoploss_order_id": "260721000423257",
  "target_trigger_price": 150.0
}
```

Response: `{"status": "success"}`, or `404` if no pending exit matches that
`instrument_key`/`stoploss_order_id` pair.

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
      "upstox_response": {}
    }
  ]
}
```

Each position is closed independently -- one failing doesn't stop the others; check each result's
own `status`. A position's own flattening order is internally sliced against its instrument's
`freeze_quantity` (same mechanism as Smart Bracket Order/Attach GTT Exits) -- transparent to this
response shape (`quantity` is still the position's full amount), but if a slice fails partway
through, that position's own `status` is `"error"` even if an earlier slice already went
through, since a half-flattened position isn't actually safe.

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

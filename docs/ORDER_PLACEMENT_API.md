# Order Placement API

Backend contract for placing app smart orders.

The app used to also have `POST /api/orders/market-bracket` (a real immediate-fill MARKET entry
with target/stoploss GTT exits attached after) plus `GET /api/orders/pending-exits` and
`PUT /api/orders/pending-exits/target-price` for viewing/editing those exits before they resolved
-- retired for being unreliable in live trading. Every order the app places now goes through
Smart Bracket Order below, a real Upstox GTT bracket. `POST /api/orders/gtt/attach-exits` (see
below) is unrelated to that retired flow -- it's a still-live fallback for attaching protection to
a position that has no GTT bracket at all (e.g. opened outside the app), and keeps using the same
underlying pending-exit/`oco_watcher` mechanism -- as does `POST /api/orders/cancel-resting-exit`
(also below), which replaced the old pending-exits lookup for the one thing that still needed it:
clearing a resting plain stoploss order before the app manually closes such a position.

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

There's no dedicated endpoint to view or edit a pending exit once armed -- the stoploss leg's own
price can still be re-pointed through the ordinary `PUT /orders/modify` below (a real order), but
the watched target price itself isn't currently exposed for editing after the fact. When the
client is about to manually close a position that might be protected this way (rather than by a
real GTT bracket), see `POST /orders/cancel-resting-exit` below.

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

## Cancel Resting Exit

```http
POST /api/orders/cancel-resting-exit
```

Best-effort cancels a still-resting plain (non-GTT) stoploss order for one instrument -- see
Attach GTT Exits above -- before the client submits a fresh opposite-side Smart Bracket Order to
manually close that position from the sticky action panel. Without this, Upstox can see more
pending exit exposure than the position actually holds and reject the fresh order for margin (a
"naked excess"), since the resting stoploss already reserves the full quantity.

A position protected by a real GTT bracket needs no equivalent call -- Upstox cleans up its own
bracket legs once the position is flattened. This only matters for a position whose protection
came from `POST /orders/gtt/attach-exits` instead, which is what `PendingOcoPairsStore` tracks.
Bulk/max-loss flattening (`POST /orders/exit-positions`/`exit-all`) already does this same
cancellation internally for every position being closed; this endpoint exposes the same
mechanism for a single manual close.

Request:

```json
{
  "instrument_key": "NSE_FO|111"
}
```

Response: always `{"status": "success"}`, whether or not anything was actually cancelled -- a
failed lookup/cancel here just means the order that follows fails exactly the way it would have
without this call.

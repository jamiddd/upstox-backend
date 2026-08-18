# Order Placement API

The GTT-based order model this document used to describe (`POST /api/orders/smart-bracket`,
`GET/PUT/DELETE /api/orders/gtt*`) was retired as part of the GTT-to-order-engine cutover. That
model placed a real Upstox GTT bracket (multi-leg conditional order watched by Upstox's own GTT
engine) for both target and stoploss at entry. It has been fully replaced by the order engine,
which manages stop-loss/target/trailing server-side instead of relying on broker-native GTT
orders.

All endpoints require:

```text
X-API-Key: <MOBILE_API_KEY>
```

## Placing, modifying, cancelling orders

```http
POST   /api/order-engine/orders
PUT    /api/order-engine/orders/{idempotency_key}/quantity
POST   /api/order-engine/orders/{idempotency_key}/cancel
GET    /api/order-engine/orders/{idempotency_key}
```

See `app/api/order_engine_routes.py` for the full request/response contract -- each route's own
doc comment covers idempotency-key semantics, the Rejected-vs-Ambiguous HTTP status contract, and
bracket (target/stoploss/trailing) parameters.

## Ledger (lots, trigger rules, max-loss epoch)

```http
GET  /api/order-engine/ledger/lots
GET  /api/order-engine/ledger/lots/{lot_id}
PUT  /api/order-engine/ledger/lots/{lot_id}/bracket
PUT  /api/order-engine/ledger/trigger-rules
PUT  /api/order-engine/ledger/trigger-rules/{rule_id}/tighten-stop-loss
POST /api/order-engine/ledger/trigger-rules/{rule_id}/cancel
GET  /api/order-engine/ledger/armed-brackets
GET  /api/order-engine/ledger/pnl-summary
GET  /api/order-engine/ledger/max-loss-epoch
PUT  /api/order-engine/ledger/max-loss-epoch
GET  /api/order-engine/engine-health
```

## Flattening positions

```http
POST /api/order-engine/exit-all
POST /api/order-engine/exit-positions
```

`POST /api/order-engine/exit-positions` optionally scopes the flatten to
`{"instrument_keys": [...]}`; omitted/null closes every open position, identical to
`/api/order-engine/exit-all`. Both operate on Upstox's own real held positions (`GET /positions`),
not just the order-engine ledger's own `lots` table, so they flatten everything actually held
regardless of which system opened it. Best-effort per position -- one instrument failing to
flatten doesn't stop the rest; check each result's own `status` field.

## Max Loss Settings

```http
GET /api/settings/max-loss
PUT /api/settings/max-loss
```

Note: as of the GTT-to-order-engine cutover, nothing in the backend background-watches this
setting anymore (the old watcher that read it, `max_loss_watcher.py`, was retired alongside the
GTT model). The order engine has its own, separate max-loss mechanism -- see the ledger's
`GET/PUT /api/order-engine/ledger/max-loss-epoch` above.

## Modify Orders

```http
PUT /api/orders/modify
```

Unrelated to the order engine specifically -- generic regular-order (non-bracket) modification,
unchanged by the cutover. See `app/api/routes.py`'s `modify_orders` for the request/response
shape.

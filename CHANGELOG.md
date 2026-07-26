# Changelog

## Unreleased

- Added durable, paginated notifications with severity/category filters, unread state, device
  preference registration, and live `/api/stream` dispatch.
- Added notifications for authentication expiry, risk-exit and GTT failures, feed health,
  repeated signal failures, account snapshots, backend restart, and filled/rejected orders.
- Added configurable daily notification retention (`NOTIFICATION_RETENTION_DAYS`, default 90).
- Added Firebase Cloud Messaging push delivery for notifications (`FIREBASE_SERVICE_ACCOUNT_PATH`) --
  a data-only push per registered device's severity preference, gracefully disabled when
  unconfigured.
- Fixed FCM push delivery failing every send as `UnregisteredError`/`NotRegistered` regardless of
  how fresh the device token was -- `fcm_service.py` was building the message with `Message.fid`,
  a distinct Firebase Installations addressing scheme in firebase-admin 7.x, not an alias for the
  classic `FirebaseMessaging.getToken()` registration token Android sends. Switched to `token=`
  (deprecated in this SDK version but still the correct field for this token type).
- Fixed the portfolio-stream-feed authorize call permanently failing with `Resource not Found`
  (surfaced to users as a recurring "Portfolio feed disconnected" notification, not a transient
  blip) -- it was requesting `/v3/feed/portfolio-stream-feed/authorize`, but Upstox never migrated
  this endpoint to v3 the way it did the market-data feed; only `/v2/...` exists. Confirmed against
  Upstox's own published docs before changing.
- Disabled the market-data feed's 30s "no frames -> reconnect" staleness watchdog for the
  portfolio feed specifically (`UpstoxWebSocketClient` now takes an opt-out `stale_after_seconds`).
  That watchdog is correct for the market-data feed's continuous tick stream, but the portfolio
  feed is purely event-driven and can go quiet for long, normal stretches with no order/position
  activity -- Upstox keeps such an idle connection alive with transport-level ping frames the
  `websockets` library answers automatically without ever surfacing to `recv()`, so the watchdog
  was forcing an endless reconnect loop on an otherwise healthy, simply idle connection. A
  genuinely dead portfolio-feed connection is still caught by the `websockets` library's own
  ping/pong keepalive.
- Configured the root logger (`logging.basicConfig(..., force=True)` in `app/main.py`) -- no prior
  config existed, so Python's root logger defaulted to `WARNING` with no handler and every
  `logger.info`/`debug` call across the whole backend was silently dropped from
  `docker compose logs`, regardless of when or how often it was checked. This was the root cause
  of several dead-end investigations before the actual bugs above were found.
- Removed the target-watcher/attach-gtt-exits recovery mechanism (`oco_watcher.py`,
  `pending_oco_pairs_store.py`, `SmartOrderService.attach_gtt_exits`/`cancel_resting_stoploss_orders`,
  `POST /orders/gtt/attach-exits`, `POST /orders/cancel-resting-exit`) -- every order now goes
  exclusively through a real Upstox GTT bracket placed atomically at entry, making the "attach
  protection to an already-open, unprotected position" fallback unnecessary. Its target-watching
  half was the single most fragile piece of the whole safety surface: it armed a target as a
  watched price level and, if the market order it fired on a cross itself failed, the position was
  already dropped from tracking with no notification at all. There is now no in-app recovery path
  for a position that somehow ends up without a bracket -- deliberate tradeoff, not an oversight.
- Added a backend-side max-loss watcher (`max_loss_watcher.py`) as a backstop alongside the app's
  own foreground, tick-driven `MainViewModel.checkMaxLoss` -- reacts even if the app is closed,
  backgrounded, or offline. Checks every 5s during market hours against a threshold synced from
  the app via new `GET`/`PUT /settings/max-loss` endpoints (`MaxLossSettingsStore`); on breach,
  flattens every position (`SmartOrderService.exit_all_positions`) and records a `risk`/critical
  notification either way -- success or a failed flatten. Shares a lock with
  `POST /orders/exit-all`/`exit-positions` (both routes now hold it too), so a client-triggered
  flatten and the watcher's own can never race into flattening the same still-open position twice.

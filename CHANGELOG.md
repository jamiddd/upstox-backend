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

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

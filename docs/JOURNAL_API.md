# Journal and Analytics API

The journal is forward-only from the first deployment with `JOURNAL_DATABASE_PATH` configured
(default `/data/journal.sqlite3`). Historical date-only Upstox trades are deliberately not
backfilled because multiple same-contract scalps cannot be sequenced safely.

All routes require `X-API-Key`.

```http
GET /api/journal/trades?page_number=1&page_size=20
GET /api/journal/trades/{id}
PATCH /api/journal/trades/{id}/notes
POST /api/journal/trades
GET /api/journal/filter-options
GET /api/analytics/summary?capital_base=100000
```

The fill ledger reconciles from Upstox's current-day trades at startup when authenticated, after
OAuth succeeds, on portfolio order events, periodically during market hours, and after close.
Missing/expired daily authentication is a waiting state, not an ingestion failure.

Automatic trades are matched flat-to-flat in FIFO order. Their stable ID is a SHA-256 fingerprint
of the allocated fill IDs, roles, and quantities. A rematch that would change an annotated trade
does not discard its notes; the existing trade becomes `needs_review` and a system warning is
recorded.

Manual creation checks same-day visible trades using normalized symbol, direction, quantity,
entry/exit prices, and execution times. It returns HTTP `409` when the trade is already journaled.
If the manual row was saved before its broker fills arrived, the next reconciliation upgrades that
same row to broker-derived execution facts and attaches the fills while preserving its journal
notes, rather than creating a second trade.

`trade_context` captures the complete raw underlying-signals response plus the traded contract LTP
after successful placement and again on a fill. Capture is asynchronous and never blocks order
placement. A failed signals request still produces a row with `{}` context.

Analytics includes closed, non-excluded trades and returns its sample size plus `low_sample=true`
below 30 trades.

## Backup

Journal notes and market context are not recoverable from the broker. Install a nightly cron entry
on the VPS which invokes `scripts/backup_journal.sh`; configure `JOURNAL_BACKUP_DESTINATION` as an
off-box `rsync` target. Verify restores periodically.

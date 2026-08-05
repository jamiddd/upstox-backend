# Technical Improvements Review

Review date: 2026-08-05

## Current status

- The project has broad service and API coverage.
- The test suite currently passes: **510 tests passed**.
- The main risks are background-task lifecycle, deployment safety, persistence behavior, and missing automated quality gates.

## Highest priority

### 1. Fix duplicated background tasks

`app/main.py` starts `journal_reconciler_task` and `journal_reconciler.reconcile()` twice around lines 425-431.

This can cause duplicate reconciliation, duplicate notifications, duplicate Upstox calls, and race conditions. The second task assignment also overwrites the first task handle, leaving one task unmanaged during shutdown.

### 2. Track and cancel every background task

`auto_login_task` is created but is not cancelled or awaited during shutdown. There are also several fire-and-forget `asyncio.create_task(...)` calls.

Use a centralized task registry or Python 3.11 `asyncio.TaskGroup` so task startup, failure handling, and shutdown are deterministic.

### 3. Add CI quality checks

There is no visible CI, linting, formatting, or type-checking configuration. Add:

- GitHub Actions
- `ruff check`
- `ruff format --check`
- `mypy` or `pyright`
- Full pytest execution
- Docker build validation

These checks should help catch duplicated startup code, formatting issues, typing errors, and broken container builds.

## Important improvements

### 4. Align development and production Python versions

The Docker image uses Python 3.12, while the local virtual environment uses Python 3.9. Tests emit Python 3.9 end-of-life warnings.

Recreate the development environment with Python 3.12 and document the supported version.

### 5. Make deployment safer

`scripts/deploy.sh` currently performs `git pull` and restarts the container without running tests, checking health, or rolling back after a failed startup.

Add preflight checks, Docker build validation, health polling against `/health`, and a clear failure/rollback strategy.

### 6. Add Docker health checks and resource limits

`docker-compose.yml` has restart behavior but no health check or CPU/memory limits.

Add an HTTP health check and reasonable resource limits so a runaway feed or background task cannot consume the VPS indefinitely.

### 7. Avoid silently falling back to `/tmp` for persistent data

`app/services/gtt_history_store.py` falls back to `/tmp/gtt_history.sqlite3` when `/data` is unavailable. In production, this can silently lose GTT history after a reboot.

Prefer failing loudly in production, or make the fallback explicitly development/test-only.

### 8. Improve observability

Add structured operational metrics for:

- Feed connections and reconnections
- Upstox API latency and error counts
- Background task failures and restarts
- Last successful poll timestamps
- SQLite size and cleanup statistics

An initial `/metrics` endpoint or OpenTelemetry integration would be useful.

### 9. Add rate limiting and request correlation IDs

The API exposes authentication and trading operations. Consider:

- Per-key/IP rate limiting
- Request IDs in logs and responses
- Idempotency keys for order placement and cancellation
- Audit logging for all trading mutations

### 10. Verify backups and restores

The project includes `scripts/backup_journal.sh`, but should also have a tested restore workflow. Backups should be atomic, retained by age/count, checked with SQLite integrity validation, and periodically restore-tested.

## Smaller cleanup items

- Remove duplicate imports and whitespace in `app/main.py`.
- Replace deprecated Firebase `Message.token` usage.
- Add lifespan startup/shutdown integration tests.
- Add concurrency tests for SQLite stores.
- Define explicit timeout and retry policies per Upstox endpoint.
- Add API schema and compatibility tests for mobile and web clients.

## Recommended implementation order

1. Remove duplicated journal reconciler startup.
2. Centralize background-task lifecycle and shutdown handling.
3. Add CI with tests, linting, formatting, typing, and Docker build checks.
4. Harden deployment and add Docker health checks.
5. Fix persistent-storage fallback behavior.
6. Add metrics, rate limiting, idempotency, and restore verification.

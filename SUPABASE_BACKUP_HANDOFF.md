# Supabase as a Nightly Backup Mirror for upstox-api

## Context

The suite today is: PersonalScalper (Android, thin Retrofit client, no local DB — only DataStore for UI prefs) and a SvelteKit web client, both talking to a single FastAPI backend (`~/upstox-api`) which is the real shared "database" for the app suite. That backend persists everything in **six separate SQLite files** under a docker volume (`/data`) — `journal.sqlite3`, `oi_snapshots.sqlite3`, `notifications.sqlite3`, `gtt_history.sqlite3`, plus candle-cache/signal-snapshot files — written via raw `sqlite3`, no ORM, no migration framework, schema created idempotently via `CREATE TABLE IF NOT EXISTS`.

The user initially asked about "one shared database for this app and future apps," which — since the backend already serves that role for every client — turned out on discussion to really mean: **add Supabase (hosted Postgres) as a disaster-recovery backup for the existing SQLite data**, not a replacement for it. `JOURNALING_ANALYTICS_HANDOFF.md` explicitly rejected Supabase/hosted Postgres as the *primary* store for good reasons (single-writer ledger, no cloud-reachability dependency during market hours); that decision stands. FastAPI/SQLite stays the live, hot-path system of record. Supabase is added purely as a nightly-refreshed, human-browsable fallback so there's something to restore from if the VPS or its data volume is ever lost — and the mechanism is built generically enough that plugging in a 7th SQLite store (for this backend or a future app's backend) later is cheap.

This document is a **planning/estimation deliverable only** — no code changes are made as part of this plan.

## Locked decisions (confirmed with user)

1. SQLite remains primary; FastAPI is the only thing that ever writes it. No dual-write, no added hot-path latency/risk.
2. Supabase mirrors the data **nightly**, as real typed/queryable Postgres tables (not opaque file blobs).
3. FastAPI/the VPS is the only thing that talks to Supabase — no Android/web client ever connects to Supabase directly (keeps existing X-API-Key/web-session auth model untouched).
4. Build this as reusable groundwork: adding a future 7th SQLite store (this backend or a future app's backend) should be cheap, not bespoke.

## Recommended approach

**New package `app/services/backup_mirror/`** in `~/upstox-api`:
- `schema_map.py` — one declarative `MirrorTableSpec` per SQLite table (column→Postgres type map, key column(s), which TEXT columns are JSON→`jsonb`, and a `strategy: "full_refresh" | "append_only_incremental"`).
- `sync_engine.py` — generic engine: opens each SQLite file **read-only** (`file:...?mode=ro`), ensures the Postgres table/columns exist (idempotent `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN IF NOT EXISTS`, with drift detection that warns instead of silently ignoring undeclared columns), then syncs rows.
- `run_nightly_sync.py` — entrypoint; loops all specs with per-table try/except so one failure doesn't abort the run; emits a critical notification via the existing `NotificationStore`/`notification_service.py` on any failure (reuses the app's existing alerting/UI path rather than a new channel).

**Sync strategy, per table, not one global rule:**
- Small/slow-changing tables (`journal_metadata`, `journal_sessions`, `trade_context`, `trade_fills`, `journal_trades`, `notifications`, `gtt_history`) — **full truncate + reload** nightly. Simple, self-healing, cheap at this volume (single-user, low daily row counts).
- `oi_snapshots`/`oi_strikes` — the one genuinely high-volume, append-only table set — **incremental** via watermark (`WHERE id > max(id) already in Supabase`, `INSERT ... ON CONFLICT DO NOTHING`).

**Connection**: direct Postgres connection (`psycopg2-binary`) using Supabase's service-role connection string, not the `supabase-py` REST/RLS client — this needs to run raw DDL/bulk DML, which the REST wrapper can't do. New secrets (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL`) added to `.env` and `app/core/config.py`'s `Settings`, following the same pattern as `MOBILE_API_KEY`/`WEB_SESSION_SECRET`.

**Scheduling**: host crontab entry (mirroring the existing, currently-uncronned `scripts/backup_journal.sh` pattern) running `docker compose exec -T api python -m app.services.backup_mirror.run_nightly_sync` after market close, logged to `/var/log/backup_mirror.log`. No new sidecar container — the sync needs the same `/data` volume and `Settings` the `api` container already has.

**Restore path**: new `scripts/restore_from_mirror.py` rebuilds a fresh `.sqlite3` file per store from the Postgres mirror, reusing each store's own original `CREATE TABLE` DDL (so the reconstructed schema matches exactly), then runs `PRAGMA integrity_check` before it's swapped into `/data`. Pointing the live app directly at Postgres as a temporary primary is explicitly **out of scope** — the stores are hand-written against SQLite-specific SQL, and making them dialect-portable would be a much larger rewrite that isn't worth it for a single-user backend's disaster-recovery case. Accepted RTO: minutes of manual restore + at-most-one-night of data loss (anything written after the last successful nightly sync).

**Known pre-existing issue to flag**: `gtt_history_store.py` silently falls back to `/tmp/gtt_history.sqlite3` if `/data` is unavailable. The sync job should detect and loudly warn if it observes this path, since mirroring `/tmp` state nightly means backing up data that isn't even durable across container restarts. Worth fixing the underlying bug before or alongside this work.

## Effort estimate

| Stage | Estimate | Reusable groundwork vs per-store |
|---|---|---|
| Supabase project + secrets/config wiring | 1-2 hrs | One-time |
| Generic `schema_map.py` + `sync_engine.py` (proven against one table) | 4-6 hrs | One-time |
| Declare specs for remaining 7 tables (incl. incremental logic for `oi_snapshots`/`oi_strikes`) | 4-6 hrs (~30-45 min/table) | Per-store, but thin |
| Cron/docker wiring (`scripts/run_backup_mirror_sync.sh` + crontab) | 1-2 hrs | One-time |
| Failure-notification integration (reuse existing `NotificationStore`) | 1-2 hrs | One-time |
| Restore script + one real restore drill | 3-5 hrs | One-time |
| End-to-end test (real nightly run, verify Supabase dashboard, force a failure, force a schema-drift case) | 3-4 hrs | One-time |
| **Total** | **~16-26 hrs (2-3 focused days)** | Adding a future 7th store later ≈ well under 1 hr |

## Explicit scope boundaries (call these out plainly, not silently drop them)

- Plain JSON files under `/data` (`watchlist.json`, `tracked_instruments.json`, etc.) and the encrypted Upstox token file are **not** schema-translated into Postgres tables — out of scope for "SQLite table → Postgres table" mirroring. If wanted later, treat them like `backup_journal.sh` does today: a plain off-box file copy, not a relational mirror.
- No client app (Android/web/future apps) ever gets a Supabase SDK or direct DB access — this is purely a backend-internal DR mechanism.
- Data loss window is up to one night (last sync → incident) — acceptable per the user's "nightly" requirement, but should be stated plainly, not assumed.

## Verification (once implemented)

1. Run `run_nightly_sync.py` manually against real (or copied) VPS data; confirm each Supabase table appears with correct types/row counts in the Supabase dashboard's table editor.
2. Force a failure (e.g. wrong `SUPABASE_DB_URL`) and confirm a critical notification appears in the existing Notifications UI/Android app, and that the live FastAPI app is completely unaffected (still serves requests normally).
3. Manually add a test column to one SQLite store's schema without updating `schema_map.py`; confirm the drift-detection warning fires instead of silently dropping the column.
4. Run `restore_from_mirror.py` against the mirrored data into a scratch file; diff row counts/checksums against the original SQLite file; confirm `PRAGMA integrity_check` passes.

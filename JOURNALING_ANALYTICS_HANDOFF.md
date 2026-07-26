# Journaling & Analytics — Handoff

Status: **planning locked, no code written yet.** This document is the single source of truth
for starting implementation cold. It assumes the reader has the original journaling/analytics
plan (backend-owned fill ledger → FIFO matcher → journal → analytics); this doc records the
**revisions agreed after review**, plus one **new requirement — market-context capture — that
was only discussed in a separate session** and appears in no other document.

Repos: backend `~/upstox-api` (FastAPI, Python 3.9, single-user, X-API-Key auth), Android
client `~/AndroidStudioProjects/PersonalScalper` (Kotlin/Compose). Backend deploys via docker
compose on a VPS; `/data` is the persistent volume.

---

## 1. Decisions locked during review (deltas from the original plan)

### Cut from MVP entirely
- **Historical backfill.** Upstox's historical trade data is date-only (no time-of-day). The
  user is a scalper — multiple round trips per contract per day is the *normal* pattern, so
  date-only fills cannot be sequenced deterministically and essentially all backfilled history
  would land in `needs_review`. The ledger starts from deploy day forward. Delete from scope:
  backfill checkpoints, date-only matching mode, most data-quality UI. Revisit only if
  historical payloads turn out to carry usable timestamps.
- **Normalized tag tables** (`tags` + `journal_trade_tags` + tag CRUD endpoints). Single user:
  a JSON array column on the notes row plus a `SELECT DISTINCT`-backed `filter-options`
  endpoint gives identical UX.
- **Optimistic locking / note-version conflict handling.** One person, one phone.
  Last-write-wins keyed on `updated_at` is sufficient.
- **Reversal split-allocation in the matcher.** The app's own flow (buy option → GTT bracket
  exits) structurally never reverses. Keep the FIFO invariant "a trade closes when exposure
  returns to zero"; if a reversal ever appears in the fill stream, flag the sequence
  `needs_review` instead of implementing exact split math.
- **Most analytics dimensions at launch.** With this user's trade frequency, month one yields
  ~40–80 trades; per-hour × per-setup × per-tag cells hold 2–3 trades and profit factor /
  expectancy at n<50 is noise. MVP analytics: summary KPIs (gross/net P&L, trade count, win
  rate, avg win/loss, best/worst), equity curve, **one** breakdown dimension (weekday or
  setup). Every figure must display its `n` with a low-sample warning below a threshold
  (suggest n<30). The remaining dimensions are trivial to add once data exists.

### Changed
- **Charges are `computed`, not `estimated`.** For index options, brokerage is flat and every
  statutory component (STT, stamp, exchange txn, SEBI, GST) is exactly computable from the
  fill — the existing `/api/charges/brokerage` logic is essentially exact. Compute
  server-side at match time, store with provenance `computed`, keep a nullable manual
  override column, and drop "estimated" labeling from the UI. (Provenance enum shrinks to
  `computed | manual`.)
- **Endpoint surface trimmed.** Target roughly: `GET /api/journal/trades`,
  `GET /api/journal/trades/{id}`, `PATCH /api/journal/trades/{id}/notes`,
  `POST /api/journal/trades` (manual), `GET /api/journal/filter-options`,
  `GET /api/analytics/summary` (+ equity curve either embedded or as one sibling route).
  No `sync-status`, no separate `adjustments` PATCH, no tag CRUD, no `recalculate` (see §1
  design constraint below).
- **Backups move from Phase 5 to Phase 1.** Recent fills are re-fetchable from the broker;
  what is irreplaceable is `journal_notes` and the **trade-context snapshots (§3)**. From the
  first deploy: nightly `sqlite3 /data/journal.sqlite3 ".backup ..."` + copy off-box (cron on
  the VPS; any off-box target is fine).
- **Equity curve / drawdown convention:** report in ₹ absolute AND as % of the app's
  capital-base setting (Android already tracks a capital base for the max-loss percent mode;
  the backend can take it as a query param or sync it like max-loss settings).

### Must-design-before-Phase-2 (hard constraint, not a task)
**Stable trade identity across re-matching.** Annotations are keyed to `journal_trade_id`; a
rebuild triggered by a late-arriving fill can change trade *boundaries* (split one trade into
two, merge two into one), orphaning notes. Required behavior:
- Trade identity = deterministic function of its allocated fill-ID set (e.g. hash of sorted
  entry+exit fill IDs). A rebuild that reproduces the same fill-set keeps the same ID.
- A rebuild that would alter the fill-set of a trade **that has annotations** must NOT
  silently proceed: keep the annotated trade, mark it `needs_review`, and surface a
  notification (existing `NotificationService`, category `system`).
- `calculation_version` is stored per trade but is bookkeeping, not the migration mechanism.

### Reconciliation & the daily token expiry
Upstox access tokens expire ~3:30 AM IST daily. Consequences:
- "Reconcile on backend startup" fails silently every morning until the user re-auths. The
  reconciler must treat missing/expired token as *skip and wait* (the existing pattern:
  `token_store.has_token()` guard, same as `max_loss_watcher` / `PositionPnlTracker`), and
  **token-becomes-valid must be a reconcile trigger** (hook the OAuth callback / token save
  path) — otherwise the first reconcile of each day happens at an unpredictable time.
- `GET /order/trades/get-trades-for-day` only covers *today*. Accepted limitation: if the
  backend is down across a full session and never fetched that day's trades, that day is
  permanently degraded (historical API is date-only). Record a `system`/`warning`
  notification when a gap day is detected; do not attempt heroic recovery.

### Database
**SQLite on the VPS. Not Supabase/hosted Postgres** — decided explicitly. Single writer,
single reader, trivial volume, and the write path of a financial ledger must not depend on an
external cloud service being reachable during market hours. New dedicated file
`/data/journal.sqlite3` (do NOT put journal tables in the notification or OI databases), WAL
mode, follow the existing store pattern (`notification_store.py` / `oi_snapshot_store.py`:
plain `sqlite3`, `busy_timeout`, schema-on-init, temp-file stores in tests). The store-layer
seam is the migration path if multi-device ever becomes real.

Timestamps: store UTC with explicit offset; compute trading-day grouping in Asia/Kolkata.
Matching policy: FIFO (locked). Analytics default population: closed, non-excluded trades.

---

## 2. What survives from the original plan (unchanged)

- Tables `trade_fills` (immutable, broker fill-ID as idempotency key, `raw_payload_json`),
  `journal_trades`, `journal_trade_fills` (allocation linkage), `journal_notes` (editable
  user fields, now including a `tags` JSON column).
- Fill ingestion triggers: backend startup (token-guarded), every portfolio-feed order event,
  periodic during market hours, once after close. The durable ledger fetches confirmed data
  from `GET /order/trades/get-trades-for-day` rather than trusting WebSocket payloads;
  the WebSocket event is only the *trigger*. Integration point already exists:
  `_on_portfolio_update` in `app/main.py` (~line 419) — it already fans out to
  `OrderFillDetector`, `PositionPnlTracker.refresh()`, and notifications; add the
  journal-reconcile task spawn there.
- Android: Journal + Analytics as new overflow-menu destinations, repository/ViewModel/DTO
  layering per existing conventions, journal detail editor with draft survival across
  rotation. `computeClosedPositionsToday()` in `MainViewModel` stays as-is during rollout and
  is replaced by journal summaries only after production verification.
- Phasing skeleton (contract → fill ledger → matcher → journal API/UI → analytics →
  hardening), with the cuts above applied. Phase 0's "real payload fixtures" requirement is
  relaxed: synthetic fixtures are acceptable for edge cases the user has never produced
  (reversals, exotic partials); real captures preferred where they exist.

---

## 3. NEW REQUIREMENT — market-context capture at order placement (discussed nowhere else)

**Motivation (user's own framing):** *"store the exact moment when we place a buy order —
what was the market condition during that time — create a huge dataset"* for later analysis
of what the trader is doing right/wrong. This was the original driver of the whole
journaling track, and it is the only part that is **lose-it-forever**: annotations can be
added next week, but the market state at the moment of entry cannot be reconstructed later.
The existing 5-minute snapshot stores (`SignalSnapshotStore`, `OISnapshotStore`, both in the
OI database) are too coarse — a scalp entry and exit can both happen inside one 5-minute
slot. **This belongs in Phase 1**, before any journal UI exists.

### When to capture
Three trigger points, one row each:
1. **`placement`** — immediately after a successful `SmartOrderService.place_bracket_order`
   (route layer or service layer, after the broker accepts). This is "the moment the trader
   decided". Keyed by the broker order ID(s) returned.
2. **`fill_entry`** — when the portfolio feed reports the entry order complete
   (`_on_portfolio_update`, the existing `is_new_fill` branch).
3. **`fill_exit`** — when an exit fill is detected (GTT leg fill, manual exit,
   max-loss flatten). Same hook; distinguishing entry vs exit can be done later during
   matching — at capture time it is fine to record trigger `fill` and let the matcher
   classify, if entry/exit distinction is awkward at that point.

### What to capture
- The **full raw response** of `UnderlyingSignalsService.get_signals(access_token,
  underlying_key=..., expiry_date=...)` (`app/services/underlying_signals_service.py:205`)
  as `context_json` — this already contains EMA9 5m/15m, VWAP, opening range, pivots, PCR,
  max pain, OI support/resistance, ATR, LTP, straddle, deltas. Store raw, don't cherry-pick
  columns now: any future metric/analysis reads the JSON; promote hot fields to real columns
  only when a query needs them.
- The **traded contract's own LTP** at capture time (the underlying signals payload carries
  the underlying's LTP, not the option's). Cheapest source: the backend's live feed already
  subscribes every open-position instrument (`FeedSubscriptionManager
  .set_open_position_instruments`) and the tracked underlyings; for `placement` the contract
  may not be subscribed yet — fall back to the quote/LTP REST call already used elsewhere,
  or accept null.
- Identifiers: broker `order_id`(s), `instrument_key` of the traded contract,
  `underlying_key`, expiry used for the signals call, `captured_at` (UTC), `trigger`.
- Do **not** compute/store the sentiment score server-side yet — the Python port of the
  Kotlin `MarketSentimentCalculator` is a separate deferred plan
  (`~/.claude/plans/agile-doodling-star.md`). The raw signals payload is sufficient to
  compute it offline later for the whole dataset retroactively.

### Schema
```sql
CREATE TABLE IF NOT EXISTS trade_context (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT NOT NULL,            -- broker order id (one row per slice is fine)
    trigger       TEXT NOT NULL,            -- 'placement' | 'fill_entry' | 'fill_exit' | 'fill'
    instrument_key TEXT NOT NULL,           -- traded contract
    underlying_key TEXT NOT NULL,
    expiry_date   TEXT,                     -- expiry used for the signals call
    contract_ltp  REAL,                     -- nullable
    captured_at   TEXT NOT NULL,            -- UTC ISO-8601
    context_json  TEXT NOT NULL,            -- raw get_signals() payload
    UNIQUE(order_id, trigger)
);
```
Lives in `/data/journal.sqlite3`. Linkage to `journal_trades` happens later through
`order_id → trade_fills → journal_trade_fills` — context capture must NOT wait for or depend
on the matcher existing. Ship capture first; it accumulates the dataset while the rest of the
journal is still being built.

### Failure posture (important)
- Capture must **never block or fail an order placement**. Fire-and-forget
  `asyncio.create_task` after the broker call returns; catch-all + `logger.warning` inside.
  `get_signals()` makes several Upstox REST calls and can take seconds — it must run after
  the placement response is already on its way back to the app.
- If the token is missing or `get_signals` fails, write the row anyway with
  `context_json = '{}'` (or a partial payload) rather than dropping it — the identifiers and
  timestamp alone still have value, and a NULL-context row is a visible data-quality marker.
- Duplicate triggers (portfolio feed can emit repeated order updates): `INSERT OR IGNORE` on
  `UNIQUE(order_id, trigger)`.

### Which underlying/expiry to use
The signals call needs the underlying + expiry. At placement time the route/service knows
the traded contract; derive `underlying_key` the same way the order-placement flow already
resolves it (the app passes it / instrument metadata has it). Use the **nearest listed
expiry** for the signals call — the same convention the dashboard's signal polling already
enforces — not the traded contract's own expiry, so context rows are comparable with the
dashboard the trader was actually looking at.

---

## 4. Suggested implementation order

1. **Journal DB + `trade_context` capture** (schema, store, the three capture hooks, tests,
   nightly backup cron). Deployable alone; starts accumulating the irreplaceable dataset
   immediately.
2. Fill ledger (`trade_fills` ingestion + reconciliation triggers incl. token-valid hook).
3. FIFO matcher + `journal_trades`/`journal_trade_fills`/`journal_notes` (+ the trade-identity
   design from §1 baked in from the start).
4. Journal API + Android journal screens.
5. Analytics (MVP scope from §1) + Android analytics screen.

## 5. Conventions to follow (established in this codebase)

- Tests: pytest + anyio, fake stores/services, temp dirs; run `python -m pytest` (full suite,
  currently 354 passing) before every commit. Route tests via the existing FastAPI test-app
  fixtures in `tests/test_routes.py`.
- New background loops are wired in `app/main.py`'s lifespan, token-guarded, with the
  `_run_*` wrapper pattern; deps injected via small dataclasses where they exceed a few args.
- Stores: one class per file in `app/services/`, config paths in `app/core/config.py`
  (`Settings`), schema created in `__init__`.
- Notifications for operator-visible failures via `NotificationService.record(...)`.
- Changelog entry per commit in `CHANGELOG.md` (Unreleased section); docs for API surface in
  `docs/ORDER_PLACEMENT_API.md`-style files.
- Android: version bump + CHANGELOG per the batch convention in the app repo's CHANGELOG
  header; verify with `./gradlew :app:compileDebugKotlin` + `testDebugUnitTest`, install via
  `./gradlew :app:installDebug`.

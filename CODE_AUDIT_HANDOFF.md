# Code Audit & Technical Handoff Report

**Target Audience:** AI Agent / Developer responsible for `upstox-api` backend codebase.  
**Project:** Upstox Scalper Backend (FastAPI, Python 3.9/3.12, SQLite, Upstox REST & WebSocket V3).  
**Date:** July 27, 2026  
**Status:** Audit completed. Verification suite (376 unit tests) passed. Issues detailed below range from broken runtime paths to live trading performance risks.

---

## Executive Summary

An audit of the codebase was conducted to identify runtime errors, design flaws, performance bottlenecks, and discrepancies with `JOURNALING_ANALYTICS_HANDOFF.md`. While the unit test suite passes cleanly (376 tests), several subtle bugs and architectural issues were discovered that only trigger under specific live execution flows.

| Issue ID | Severity | File / Component | Summary | Action Required |
|---|---|---|---|---|
| **BUG-01** | 🔴 **Critical** | `app/services/journal_store.py` | Indentation bug makes `latest_placement_context` a module-level function instead of a `JournalStore` method. | **Must Fix** (Causes `AttributeError` at runtime) |
| **PERF-01** | 🔴 **Critical** | `app/services/journal_reconciler.py` | Reconciler makes redundant Upstox API calls for every trade of the day every minute without checking if fills already exist. | **Must Fix** (Breaches API rate limits in live trading) |
| **MEM-01** | 🟡 **Medium** | `app/services/live_candle_builder.py` | `clear(instrument_key)` is never called when instruments are unsubscribed, leading to unbounded dict growth. | **Recommended** (Low effort, prevents memory leak) |
| **RACE-01** | 🟡 **Medium** | `app/main.py` / `app/services/trade_context_service.py` | Portfolio fill event can race placement context capture, causing missing market-context rows. | **Recommended** (Improves context data quality) |
| **GAP-01** | 🟢 **Low** | `app/services/journal_store.py` | Missing API endpoint/workflow to resolve `needs_review` trades flagged by the FIFO matcher. | **Feature Gap** (Enhancement) |
| **GAP-02** | 🟢 **Low** | `app/services/journal_store.py` | Analytics summary missing equity drawdown % of capital base. | **Feature Gap** (Enhancement) |
| **GAP-03** | 🟢 **Low** | `scripts/` / Docker | Missing nightly off-box SQLite backup script. | **Operational Gap** (DevOps) |
| **WARN-01**| 🟢 **Low** | `app/services/fcm_service.py` | Deprecated Firebase `messaging.Message(token=...)` syntax. | **Maintenance** (Cleanup) |

---

## Detailed Findings & Actionable Specs

### 1. [BUG-01] Indentation Error in `JournalStore.latest_placement_context`
* **File:** `app/services/journal_store.py` (lines 760–779)
* **Invocation Point:** `app/services/trade_context_service.py` (line 97)

#### Root Cause
In `journal_store.py`, `latest_placement_context` is defined at lines 760–779 with 4 spaces of indentation, placing it outside the `JournalStore` class (which ends at line 647):

```python
# Lines 758-761 in app/services/journal_store.py:
        sequence = []
    return results

    def latest_placement_context(self, instrument_key: str) -> Optional[dict[str, Any]]:
```

#### Runtime Impact
When `TradeContextService.capture_fill_from_placement()` runs (triggered by an order fill event where no exact `order_id` placement row exists), it calls `self.store.latest_placement_context(instrument_key)`. This raises:
```text
AttributeError: 'JournalStore' object has no attribute 'latest_placement_context'
```

#### Recommendation
Indent `latest_placement_context` into the `JournalStore` class body (e.g. after `record_context` or `get_context`). Add a unit test in `test_journal_store.py` specifically asserting `journal_store.latest_placement_context(...)`.

---

### 2. [PERF-01] Redundant Upstox API Charge Queries in `JournalReconciler`
* **File:** `app/services/journal_reconciler.py` (lines 40–55)

#### Root Cause
In `JournalReconciler.reconcile()`, the loop iterates over all trades returned by `get_trades_for_day()` and computes charges before upserting:

```python
for raw in trades:
    fill = _normalize_fill(raw, fallback_date=trading_date)
    ...
    fill["computed_charges"] = await self._charges(access_token, fill) # Makes Upstox REST call
    inserted += int(self.store.upsert_fill(fill))
```

#### Runtime Impact
1. `_charges()` makes an HTTP call to Upstox `/charges/brokerage` for **every trade** returned by `get_trades_for_day()`, even if that fill was already reconciled and stored in SQLite earlier in the day.
2. `reconcile()` is triggered **every 60 seconds during market hours** and **on every portfolio order update event**.
3. If a trader executes 30 scalp trades in a day, `reconcile()` will make 30 sequential HTTP requests every minute. This will rapidly exhaust Upstox REST rate limits, causing `429 Too Many Requests` or `423 Locked` errors that block active trading routes (e.g. order placement, position exits).

#### Recommendation
Option A (Preferred): Check if the fill already exists in `trade_fills` before calling `self._charges()`. If `store.has_fill(fill["fill_id"])` returns `True`, reuse the existing `computed_charges`.  
Option B: Implement local charge calculation for index options (flat brokerage + statutory tax formulas) as outlined in Section 1 of `JOURNALING_ANALYTICS_HANDOFF.md`.

---

### 3. [MEM-01] Uncleaned State in `LiveCandleBuilder`
* **File:** `app/services/live_candle_builder.py` (lines 118–121)

#### Root Cause
`LiveCandleBuilder` maintains `self._current: dict[str, FeedCandle]` to build live 1-minute OHLC candles. Method `clear(instrument_key)` is implemented to remove unsubscribed instruments, but it is **never called** in `app/main.py` or `FeedSubscriptionManager` when open positions close or tracked underlyings change.

#### Impact
In-memory dictionary entries remain forever for all option contracts traded throughout the session.

#### Recommendation
Hook `candle_builder.clear(key)` inside `FeedSubscriptionManager` or `_on_portfolio_update` when an instrument is unsubscribed from the market feed.

---

### 4. [RACE-01] Order Placement Context Capture Race Condition
* **File:** `app/main.py` (lines 570–582) & `app/services/trade_context_service.py` (lines 95–100)

#### Root Cause
In `place_smart_bracket_order` (`app/api/routes.py`, line 997), market context capture is launched as a fire-and-forget background task (`asyncio.create_task(context_service.capture(...))`).  
If the broker executes the order instantly and the portfolio feed WebSocket emits an order fill update before `capture(...)` completes its multi-step REST calls (fetching underlying signals & quotes), `_capture_fill_context()` runs `get_context(order_id, "placement")` and gets `None`.

#### Recommendation
Await the `trade_context` row insertion for placement (or pre-insert the `order_id` row stub synchronously before launching signal enrichment) so the placement correlation row is guaranteed to exist when the fill event arrives.

---

### 5. [GAP-01 through GAP-03] Roadmap & Handoff Discrepancies

1. **`needs_review` Resolution Flow**:
   * `app/services/journal_store.py` (line 331) updates trade status to `needs_review` when late fills alter an annotated trade's boundary.
   * Add a `POST /api/journal/trades/{id}/resolve` endpoint to allow users to review conflicts and mark trades resolved.
2. **Equity Drawdown %**:
   * Section 1 of `JOURNALING_ANALYTICS_HANDOFF.md` specifies reporting drawdown in ₹ and as `% of capital base`.
   * Update `JournalStore.analytics_summary()` to compute peak-to-trough drawdown values alongside `equity_curve`.
3. **SQLite Backups**:
   * Create a cron script in `scripts/backup_journal.sh` to run `sqlite3 /data/journal.sqlite3 ".backup /data/backups/journal_$(date +%Y%m%d).sqlite3"`.

---

## Suggested Fix Checklist for Next Agent

- [ ] **Fix BUG-01**: Move `def latest_placement_context` inside `class JournalStore:` in `app/services/journal_store.py`.
- [ ] **Fix PERF-01**: Add existing fill check in `JournalReconciler.reconcile` (`app/services/journal_reconciler.py`) before calling `_charges()`.
- [ ] **Fix MEM-01**: Call `candle_builder.clear()` when unsubscribing instruments.
- [ ] **Fix RACE-01**: Synchronize order placement ID registration before emitting WebSocket updates.
- [ ] Run `PYTHONPATH=. pytest` to ensure all 376+ tests pass.

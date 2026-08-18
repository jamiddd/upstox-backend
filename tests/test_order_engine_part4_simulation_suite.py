from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_order_engine_ledger_store
from app.core.config import Settings, get_settings
from app.main import app
from app.services import order_engine_max_loss_watcher as watcher
from app.services.exit_reconciliation_checker import ExitReconciliationChecker
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker
from app.services.order_engine_order_service import OrderEngineOrderService
from app.services.order_history_recorder import OrderHistoryRecorder
from app.main import _record_order_history_from_push

"""§8.4 milestone 7 -- §6.6-style simulation/fault-injection coverage extended to Part 4's own new
pieces, same discipline every earlier simulation suite in this repo already follows (drive real
production classes end to end, never a parallel model). Three scenarios, matching the milestone's
own named list:

1. A server ledger write failure mid-flow (a malformed request never reaches the ledger at all --
   confirms no partial/corrupt row is ever written).
2. Both reconciliation outcomes (`matched`/`server_vs_client_mismatch`) end to end through the real
   `ExitReconciliationChecker`, confirming the append-only event log records each correctly.
3. A max-loss breach detected *purely server-side*, with no client involved at all -- the real
   `OrderEngineLedgerStore`/`OrderEngineLotTracker`/`order_engine_max_loss_watcher` wired together,
   nothing standing in for a phone anywhere in the chain.
"""

_IST = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN_NOW = datetime(2026, 7, 21, 10, 0, tzinfo=_IST)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/token.enc"),
        order_engine_ledger_database_path=tmp_path / "ledger.sqlite3",
    )


class _FakeUpstox:
    def __init__(self, order_book_data=None, held_quantities=None) -> None:
        self.order_book_data = order_book_data if order_book_data is not None else []
        self.held_quantities = held_quantities if held_quantities is not None else {}
        self.place_order_calls: list[dict] = []

    async def get_order_book(self, access_token):
        return {"status": "success", "data": self.order_book_data}

    async def get_positions(self, access_token):
        return {
            "status": "success",
            "data": [
                {"instrument_token": key, "quantity": quantity}
                for key, quantity in self.held_quantities.items()
            ],
        }

    async def get_quotes(self, access_token, instrument_key):
        # No lot in this suite has ever ticked, so _current_equity's worst-case-pessimism pass
        # calls this for every open lot -- an empty book resolves to None, i.e. the pre-existing
        # zero-fallback for that lot, same as before this pass existed.
        return {"status": "success", "data": {instrument_key: {"depth": {"buy": [], "sell": []}}}}

    async def place_order(self, access_token, **kwargs):
        self.place_order_calls.append(kwargs)
        return {"status": "success", "data": {"order_id": f"order-{len(self.place_order_calls)}"}}


class _FakeTokenStore:
    def has_token(self) -> bool:
        return True

    def load_access_token(self) -> str:
        return "upstox-token"


class _FakeNotificationService:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def record(self, *, category, severity, title, message, details=None) -> None:
        self.records.append({"severity": severity, "title": title})


# -- Scenario 1: a malformed ledger write never lands a partial/corrupt row --------------------


def test_a_malformed_ledger_write_is_rejected_and_leaves_no_row_behind(tmp_path) -> None:
    """`PUT /ledger/lots` was removed in Part 5 (docs/ORDER_HISTORY_V2_DESIGN.md) -- `lots` is now
    server-derived, not client-upserted, so this scenario now exercises the still-client-driven
    `PUT /ledger/trigger-rules` route instead, same "a malformed request never reaches the store"
    intent as before."""
    settings = _settings(tmp_path)
    ledger = OrderEngineLedgerStore(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    client = TestClient(app)
    try:
        # rule_id must be non-empty -- this request fails Pydantic validation before it ever
        # reaches OrderEngineLedgerStore.upsert_trigger_rule at all.
        response = client.put(
            "/api/order-engine/ledger/trigger-rules",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "rule_id": "",
                "instrument_key": "NSE_FO|1",
                "state": "ARMED",
            },
        )
        assert response.status_code == 422

        # Confirms the mid-flow failure left nothing behind -- not a partial row, not any row.
        assert ledger.get_trigger_rule("") is None
    finally:
        app.dependency_overrides.clear()


# -- Scenario 2: both reconciliation outcomes, end to end ---------------------------------------


@pytest.mark.anyio
async def test_reconciliation_records_a_matched_event_when_broker_and_ledger_agree(tmp_path) -> None:
    ledger = OrderEngineLedgerStore(_settings(tmp_path))
    ledger.upsert_lot(
        lot_id="lot-match", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=0, realized_pnl=250.0, state="CLOSED",
    )
    upstox = _FakeUpstox(order_book_data=[
        {"order_id": "exit-1", "average_price": 105.0, "filled_quantity": 50},
    ])
    checker = ExitReconciliationChecker(ledger, upstox)

    result = await checker.check_lot("token", "lot-match", "exit-1")

    assert result.outcome == "matched"
    events = ledger.get_events_for_lot("lot-match")
    assert events[-1]["event_type"] == "RECONCILIATION_MATCHED"


@pytest.mark.anyio
async def test_reconciliation_records_a_mismatch_event_when_ledger_disagrees_with_broker(tmp_path) -> None:
    ledger = OrderEngineLedgerStore(_settings(tmp_path))
    # Client mirrored a wrong number -- broker truth says 250, ledger says 999.
    ledger.upsert_lot(
        lot_id="lot-mismatch", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=0, realized_pnl=999.0, state="CLOSED",
    )
    upstox = _FakeUpstox(order_book_data=[
        {"order_id": "exit-1", "average_price": 105.0, "filled_quantity": 50},
    ])
    checker = ExitReconciliationChecker(ledger, upstox)

    result = await checker.check_lot("token", "lot-mismatch", "exit-1")

    assert result.outcome == "server_vs_client_mismatch"
    assert result.is_mismatch
    events = ledger.get_events_for_lot("lot-mismatch")
    assert events[-1]["event_type"] == "RECONCILIATION_MISMATCH"
    assert events[-1]["payload_json"] is not None  # the mismatch amounts are recorded, not just a bare flag


# -- Scenario 3: a max-loss breach detected purely server-side, no client involved at all -------


@pytest.mark.anyio
async def test_a_max_loss_breach_is_caught_and_flattened_with_zero_client_involvement(tmp_path) -> None:
    # Nothing in this test constructs a BackendFeedClient, a Kotlin class, or any client-side
    # state at all -- every piece here is the real backend production wiring, driven purely by a
    # ledger write (standing in for "the client mirrored a lot at some point in the past") and a
    # single check_now call (standing in for "a live tick arrived").
    watcher._worst_case_quote_cache.clear()  # see test_order_engine_max_loss_watcher.py's own note
    ledger = OrderEngineLedgerStore(_settings(tmp_path))
    tracker = OrderEngineLotTracker(ledger)
    upstox = _FakeUpstox(held_quantities={"NSE_FO|1": 50})
    order_service = OrderEngineOrderService(upstox)
    token_store = _FakeTokenStore()
    notifications = _FakeNotificationService()
    lock = asyncio.Lock()

    ledger.upsert_max_loss_epoch(
        opening_balance=100000.0, peak_equity=100000.0, threshold_x=1000.0,
        epoch_started_at="2026-07-21T00:00:00+00:00",
    )
    # realized_pnl -2500 -> equity 97500, opening - X = 99000 -> breached, no tick/quote needed
    # since realized P&L alone already crosses the floor.
    ledger.upsert_lot(
        lot_id="lot-breach", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50, realized_pnl=-2500.0, state="OPEN",
    )

    await watcher.check_now(
        now=_MARKET_OPEN_NOW, token_store=token_store, ledger_store=ledger, lot_tracker=tracker,
        order_engine_order_service=order_service, notification_service=notifications, exit_all_lock=lock,
    )

    # The breach was caught and a real flatten order was placed -- entirely server-side.
    assert len(upstox.place_order_calls) == 1
    assert upstox.place_order_calls[0]["instrument_key"] == "NSE_FO|1"
    assert upstox.place_order_calls[0]["transaction_type"] == "SELL"

    epoch = ledger.get_max_loss_epoch()
    assert epoch["opening_balance"] == 97500.0  # auto re-armed at post-flatten equity

    assert len(notifications.records) == 1
    assert notifications.records[0]["severity"] == "critical"


# -- Scenario 4 (Part 5): one simulated fill produces consistent lots + order_history -----------


def test_one_simulated_entry_then_exit_fill_produces_consistent_lot_and_order_history(tmp_path) -> None:
    """docs/ORDER_HISTORY_V2_DESIGN.md's own end-to-end check: a real `OrderHistoryRecorder`
    driven by two confirmed broker order-book rows (an entry fill, then an exit fill) must leave
    `order_history` with one row per broker order and `lots` with a single, correctly-derived
    row -- the two writes this feature makes must never disagree with each other."""
    ledger = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(ledger)

    entry_order = {
        "order_id": "broker-entry", "tag": "tagentry", "instrument_token": "NSE_FO|1",
        "trading_symbol": "NIFTY", "transaction_type": "BUY", "product": "I",
        "order_type": "MARKET", "quantity": 50, "status": "complete",
        "average_price": 100.0, "filled_quantity": 50,
    }
    # The lot doesn't exist yet when the entry order is first sighted -- record_order_snapshot
    # must not reference a lot_id the FK can't yet satisfy, same ordering the real
    # `_record_order_history_from_push` wiring follows.
    recorder.record_order_snapshot(entry_order, idempotency_key="rule-entry", role="ENTRY")
    lot_after_entry = recorder.apply_fill_to_ledger(entry_order, lot_id="lot-1", role="ENTRY")
    recorder.record_order_snapshot(entry_order, idempotency_key="rule-entry", lot_id="lot-1", role="ENTRY")

    exit_order = {
        "order_id": "broker-exit", "tag": "tagexit", "instrument_token": "NSE_FO|1",
        "trading_symbol": "NIFTY", "transaction_type": "SELL", "product": "I",
        "order_type": "MARKET", "quantity": 50, "status": "complete",
        "average_price": 110.0, "filled_quantity": 50,
    }
    recorder.record_order_snapshot(exit_order, idempotency_key="rule-exit", lot_id="lot-1", role="EXIT")
    lot_after_exit = recorder.apply_fill_to_ledger(
        exit_order, lot_id="lot-1", role="EXIT", entry_transaction_type="BUY",
    )

    # order_history: one row per broker order, never merged.
    history_rows = {row["broker_order_id"] for row in ledger.list_orders(limit=10)}
    assert history_rows == {"broker-entry", "broker-exit"}
    assert ledger.get_order_by_broker_order_id("broker-entry")["role"] == "ENTRY"
    assert ledger.get_order_by_broker_order_id("broker-exit")["role"] == "EXIT"

    # lots: a single lot, correctly opened then closed -- consistent with both order_history rows.
    assert lot_after_entry["id"] == "lot-1"
    assert lot_after_entry["state"] == "OPEN"
    assert lot_after_exit["id"] == "lot-1"
    assert lot_after_exit["state"] == "CLOSED"
    assert lot_after_exit["remaining_quantity"] == 0
    assert lot_after_exit["realized_pnl"] == 500.0  # (110 - 100) * 50
    assert ledger.get_all_lots() == [ledger.get_lot("lot-1")]


# -- Scenario 5 (Part 5 entry-correlation fix): a real WS push auto-creates a lot for an entry --


@pytest.mark.anyio
async def test_a_real_ws_push_for_a_correlated_entry_order_auto_creates_a_lot(tmp_path) -> None:
    """Drives the *real* `app.main._record_order_history_from_push` (not a hand-rolled stand-in)
    end to end: `place_order_engine_order`'s own placement-time correlation write
    (`OrderHistoryRecorder.record_placement`) is simulated first (same thing the real route does
    right after a successful placement), then a portfolio-feed WS push for that order_id arrives
    and must resolve role=ENTRY purely from the correlation row (no PLACED trigger rule exists at
    all here), auto-creating the lot -- closing Part 5's own named entry-correlation gap."""
    ledger = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(ledger)
    recorder.record_placement(
        broker_order_id="broker-entry-real", order_tag="tagentryreal",
        idempotency_key="idem-entry-real", role="ENTRY", instrument_key="NSE_FO|1",
        transaction_type="BUY", product="I", order_type="MARKET",
        requested_quantity=50, requested_price=None, trigger_price=None,
    )

    upstox = _FakeUpstox(order_book_data=[
        {
            "order_id": "broker-entry-real", "tag": "tagentryreal", "instrument_token": "NSE_FO|1",
            "trading_symbol": "NIFTY", "transaction_type": "BUY", "product": "I",
            "order_type": "MARKET", "quantity": 50, "status": "complete",
            "average_price": 100.0, "filled_quantity": 50,
        },
    ])
    token_store = _FakeTokenStore()
    push_payload = {"order_id": "broker-entry-real", "status": "complete", "tag": "tagentryreal"}

    await _record_order_history_from_push(
        ledger, recorder, upstox, token_store, push_payload, _FakeNotificationService(),
    )

    lot = ledger.get_lot("idem-entry-real")
    assert lot is not None
    assert lot["state"] == "OPEN"
    assert lot["entry_price"] == 100.0
    assert lot["remaining_quantity"] == 50

    history_row = ledger.get_order_by_broker_order_id("broker-entry-real")
    assert history_row["status"] == "complete"
    assert history_row["lot_id"] == "idem-entry-real"
    assert history_row["role"] == "ENTRY"


# -- Scenario 6: external-order detection, entry side (no existing lot) ------------------------


@pytest.mark.anyio
async def test_an_untagged_fill_with_no_open_lot_auto_creates_an_entry_with_default_bracket(
    tmp_path,
) -> None:
    """A fill placed directly in Upstox's own app -- no tag, no record_placement row -- with no
    lot this engine already tracks for the instrument. Unambiguous: must be a fresh entry."""
    ledger = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(ledger)
    upstox = _FakeUpstox(order_book_data=[
        {
            "order_id": "broker-external-entry", "tag": None, "instrument_token": "NSE_FO|1",
            "trading_symbol": "NIFTY", "transaction_type": "BUY", "product": "I",
            "order_type": "MARKET", "quantity": 50, "status": "complete",
            "average_price": 200.0, "filled_quantity": 50,
        },
    ])
    token_store = _FakeTokenStore()
    notifications = _FakeNotificationService()
    push_payload = {"order_id": "broker-external-entry", "status": "complete"}

    await _record_order_history_from_push(ledger, recorder, upstox, token_store, push_payload, notifications)

    open_lots = ledger.get_open_lots_for_instrument("NSE_FO|1")
    assert len(open_lots) == 1
    lot = open_lots[0]
    assert lot["transaction_type"] == "BUY"
    assert lot["entry_price"] == 200.0
    assert lot["target_price"] == pytest.approx(210.0)
    assert lot["stoploss_price"] == pytest.approx(190.0)
    assert notifications.records == []


# -- Scenario 7: external-order detection, exit side (Option A) --------------------------------


@pytest.mark.anyio
async def test_an_untagged_closing_fill_against_the_single_open_lot_auto_closes_it(tmp_path) -> None:
    """Exactly one open lot on the opposite side of the fill's own transaction_type --
    unambiguous, safe to auto-match and close, per the user's own explicit 2026-08-18 decision
    (Option A: auto-match only when there is exactly one candidate)."""
    ledger = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(ledger)
    ledger.upsert_lot(
        lot_id="lot-open-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=200.0, entry_quantity=50, remaining_quantity=50, realized_pnl=0.0, state="OPEN",
    )
    upstox = _FakeUpstox(order_book_data=[
        {
            "order_id": "broker-external-exit", "tag": None, "instrument_token": "NSE_FO|1",
            "trading_symbol": "NIFTY", "transaction_type": "SELL", "product": "I",
            "order_type": "MARKET", "quantity": 50, "status": "complete",
            "average_price": 220.0, "filled_quantity": 50,
        },
    ])
    token_store = _FakeTokenStore()
    notifications = _FakeNotificationService()
    push_payload = {"order_id": "broker-external-exit", "status": "complete"}

    await _record_order_history_from_push(ledger, recorder, upstox, token_store, push_payload, notifications)

    lot = ledger.get_lot("lot-open-1")
    assert lot["state"] == "CLOSED"
    assert lot["remaining_quantity"] == 0
    assert lot["realized_pnl"] == pytest.approx(1000.0)  # (220 - 200) * 50, long lot
    assert notifications.records == []


@pytest.mark.anyio
async def test_an_untagged_closing_fill_against_multiple_open_lots_is_flagged_not_guessed(
    tmp_path,
) -> None:
    """Two open lots on the opposite side of the fill -- genuinely ambiguous which one this fill
    closes. Per the user's own explicit call: never FIFO-guess here, flag it and leave both lots
    untouched."""
    ledger = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(ledger)
    ledger.upsert_lot(
        lot_id="lot-open-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=200.0, entry_quantity=50, remaining_quantity=50, realized_pnl=0.0, state="OPEN",
    )
    ledger.upsert_lot(
        lot_id="lot-open-2", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=210.0, entry_quantity=50, remaining_quantity=50, realized_pnl=0.0, state="OPEN",
    )
    upstox = _FakeUpstox(order_book_data=[
        {
            "order_id": "broker-ambiguous-exit", "tag": None, "instrument_token": "NSE_FO|1",
            "trading_symbol": "NIFTY", "transaction_type": "SELL", "product": "I",
            "order_type": "MARKET", "quantity": 50, "status": "complete",
            "average_price": 220.0, "filled_quantity": 50,
        },
    ])
    token_store = _FakeTokenStore()
    notifications = _FakeNotificationService()
    push_payload = {"order_id": "broker-ambiguous-exit", "status": "complete"}

    await _record_order_history_from_push(ledger, recorder, upstox, token_store, push_payload, notifications)

    assert ledger.get_lot("lot-open-1")["state"] == "OPEN"
    assert ledger.get_lot("lot-open-2")["state"] == "OPEN"
    assert len(notifications.records) == 1
    assert notifications.records[0]["title"] == "Trade needs review"

    pending = ledger.get_pending_ambiguous_fills()
    assert len(pending) == 1
    assert pending[0]["order_id"] == "broker-ambiguous-exit"
    assert sorted(pending[0]["candidate_lot_ids"]) == ["lot-open-1", "lot-open-2"]


def test_resolving_a_pending_ambiguous_fill_closes_the_chosen_lot_and_clears_the_pending_row(
    tmp_path,
) -> None:
    """The Positions-screen UI is the only path that can act on an ambiguous external exit -- this
    exercises that resolve route end to end: picking one candidate closes exactly that lot, leaves
    the other untouched, and the pending row is gone afterwards (so a second resolve attempt 404s,
    never double-acts)."""
    settings = _settings(tmp_path)
    ledger = OrderEngineLedgerStore(settings)
    ledger.upsert_lot(
        lot_id="lot-open-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=200.0, entry_quantity=50, remaining_quantity=50, realized_pnl=0.0, state="OPEN",
    )
    ledger.upsert_lot(
        lot_id="lot-open-2", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=210.0, entry_quantity=50, remaining_quantity=50, realized_pnl=0.0, state="OPEN",
    )
    ledger.record_pending_ambiguous_fill(
        order_id="broker-ambiguous-exit",
        instrument_key="NSE_FO|1",
        candidate_lot_ids=["lot-open-1", "lot-open-2"],
        broker_order={
            "order_id": "broker-ambiguous-exit", "instrument_token": "NSE_FO|1",
            "transaction_type": "SELL", "average_price": 220.0, "filled_quantity": 50,
            "status": "complete",
        },
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    client = TestClient(app)
    try:
        pending_id = ledger.get_pending_ambiguous_fills()[0]["id"]
        response = client.post(
            f"/api/order-engine/ledger/pending-ambiguous-fills/{pending_id}/resolve",
            json={"lot_id": "lot-open-1"},
            headers={"X-API-Key": "mobile-secret"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"resolved": True, "lot_id": "lot-open-1"}

        assert ledger.get_lot("lot-open-1")["state"] == "CLOSED"
        assert ledger.get_lot("lot-open-2")["state"] == "OPEN"
        assert ledger.get_pending_ambiguous_fills() == []

        second_attempt = client.post(
            f"/api/order-engine/ledger/pending-ambiguous-fills/{pending_id}/resolve",
            json={"lot_id": "lot-open-1"},
            headers={"X-API-Key": "mobile-secret"},
        )
        assert second_attempt.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_an_untagged_same_side_fill_against_an_open_lot_is_left_alone(tmp_path) -> None:
    """A same-direction fill (adding to an existing position, e.g. a same-strike add on a
    fast-moving expiry day) is not a closing trade -- deliberately left undetected, same posture
    as the entry side's own "only the first fill auto-detects" scoping."""
    ledger = OrderEngineLedgerStore(_settings(tmp_path))
    recorder = OrderHistoryRecorder(ledger)
    ledger.upsert_lot(
        lot_id="lot-open-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=200.0, entry_quantity=50, remaining_quantity=50, realized_pnl=0.0, state="OPEN",
    )
    upstox = _FakeUpstox(order_book_data=[
        {
            "order_id": "broker-same-side-add", "tag": None, "instrument_token": "NSE_FO|1",
            "trading_symbol": "NIFTY", "transaction_type": "BUY", "product": "I",
            "order_type": "MARKET", "quantity": 25, "status": "complete",
            "average_price": 205.0, "filled_quantity": 25,
        },
    ])
    token_store = _FakeTokenStore()
    notifications = _FakeNotificationService()
    push_payload = {"order_id": "broker-same-side-add", "status": "complete"}

    await _record_order_history_from_push(ledger, recorder, upstox, token_store, push_payload, notifications)

    lot = ledger.get_lot("lot-open-1")
    assert lot["state"] == "OPEN"
    assert lot["remaining_quantity"] == 50  # untouched, not re-averaged
    assert notifications.records == []

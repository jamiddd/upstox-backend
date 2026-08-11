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
    settings = _settings(tmp_path)
    ledger = OrderEngineLedgerStore(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_order_engine_ledger_store] = lambda: ledger
    client = TestClient(app)
    try:
        # entry_quantity must be > 0 -- this request fails Pydantic validation before it ever
        # reaches OrderEngineLedgerStore.upsert_lot at all.
        response = client.put(
            "/api/order-engine/ledger/lots",
            headers={"X-API-Key": "mobile-secret"},
            json={
                "lot_id": "lot-bad-write",
                "instrument_key": "NSE_FO|1",
                "transaction_type": "BUY",
                "entry_quantity": 0,
                "remaining_quantity": 50,
                "state": "OPEN",
            },
        )
        assert response.status_code == 422

        # Confirms the mid-flow failure left nothing behind -- not a partial row, not any row.
        assert ledger.get_lot("lot-bad-write") is None
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

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.services import order_engine_max_loss_watcher as watcher
from app.services.order_engine_ledger_store import OrderEngineLedgerStore
from app.services.order_engine_lot_tracker import OrderEngineLotTracker
from app.services.order_engine_order_service import OrderEngineOrderService

_IST = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN_NOW = datetime(2026, 7, 21, 10, 0, tzinfo=_IST)  # a Tuesday, mid-session
_MARKET_CLOSED_NOW = datetime(2026, 7, 21, 20, 0, tzinfo=_IST)


def _settings(tmp_path) -> Settings:
    return Settings(
        upstox_api_key="key",
        upstox_api_secret="secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile",
        token_encryption_key="test-key",
        token_store_path=tmp_path / "token.enc",
        order_engine_ledger_database_path=tmp_path / "order_engine_ledger.sqlite3",
    )


class _FakeTokenStore:
    def __init__(self, *, has_token: bool = True) -> None:
        self._has_token = has_token

    def has_token(self) -> bool:
        return self._has_token

    def load_access_token(self) -> str:
        return "upstox-token"


class _FakeNotificationService:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def record(self, *, category, severity, title, message, details=None) -> None:
        self.records.append({"category": category, "severity": severity, "title": title, "message": message})


class _FakeUpstox:
    """[held_quantities] backs `get_positions` -- `{instrument_key: signed_quantity}`, exactly
    what `resolve_closeable_quantity` matches against. A test not exercising
    `flatten_open_lots`'s broker-truth cap should pass a quantity comfortably >= whatever the
    lot's own `remaining_quantity` is, so the cap is a no-op there; a test exercising the cap
    itself passes something smaller. There's no implicit "unlimited" default -- real Upstox has
    no such thing either (an instrument missing from the response is just flat/zero, exactly what
    [resolve_closeable_quantity] would derive from an empty match)."""

    def __init__(self, fail_instrument_keys: set[str] | None = None, held_quantities: dict[str, float] | None = None) -> None:
        self.place_order_calls: list[dict] = []
        self._fail_instrument_keys = fail_instrument_keys or set()
        self._held_quantities = held_quantities or {}

    async def get_order_book(self, access_token):
        return {"status": "success", "data": []}

    async def get_positions(self, access_token):
        return {
            "status": "success",
            "data": [
                {"instrument_token": key, "quantity": quantity}
                for key, quantity in self._held_quantities.items()
            ],
        }

    async def place_order(self, access_token, **kwargs):
        if kwargs.get("instrument_key") in self._fail_instrument_keys:
            raise RuntimeError("simulated broker failure")
        self.place_order_calls.append(kwargs)
        return {"status": "success", "data": {"order_id": f"order-{len(self.place_order_calls)}"}}


def _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY", entry_price=100.0, remaining_quantity=50, realized_pnl=0.0):
    store.upsert_lot(
        lot_id=lot_id, instrument_key=instrument_key, transaction_type=transaction_type,
        entry_price=entry_price, entry_quantity=remaining_quantity, remaining_quantity=remaining_quantity,
        realized_pnl=realized_pnl, state="OPEN",
    )


def _watcher_deps(tmp_path, *, fail_instrument_keys=None, has_token=True, held_quantities=None):
    store = OrderEngineLedgerStore(_settings(tmp_path))
    tracker = OrderEngineLotTracker(store)
    upstox = _FakeUpstox(fail_instrument_keys=fail_instrument_keys, held_quantities=held_quantities)
    order_service = OrderEngineOrderService(upstox)
    token_store = _FakeTokenStore(has_token=has_token)
    notifications = _FakeNotificationService()
    lock = asyncio.Lock()
    return store, tracker, upstox, order_service, token_store, notifications, lock


@pytest.mark.anyio
async def test_check_now_is_a_no_op_when_market_is_closed(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(tmp_path)
    store.upsert_max_loss_epoch(opening_balance=100000.0, peak_equity=100000.0, threshold_x=1000.0, epoch_started_at="2026-07-21T00:00:00+00:00")
    _open_lot(store, realized_pnl=-5000.0)  # would otherwise breach

    await watcher.check_now(
        now=_MARKET_CLOSED_NOW, token_store=token_store, ledger_store=store, lot_tracker=tracker,
        order_engine_order_service=order_service, notification_service=notifications, exit_all_lock=lock,
    )

    assert upstox.place_order_calls == []


@pytest.mark.anyio
async def test_check_now_is_a_no_op_when_no_epoch_has_been_started(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(tmp_path)
    _open_lot(store)

    await watcher.check_now(
        now=_MARKET_OPEN_NOW, token_store=token_store, ledger_store=store, lot_tracker=tracker,
        order_engine_order_service=order_service, notification_service=notifications, exit_all_lock=lock,
    )

    assert upstox.place_order_calls == []


@pytest.mark.anyio
async def test_check_now_ratchets_the_peak_without_flattening_on_a_non_breaching_gain(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(tmp_path)
    store.upsert_max_loss_epoch(opening_balance=100000.0, peak_equity=100000.0, threshold_x=1000.0, epoch_started_at="2026-07-21T00:00:00+00:00")
    _open_lot(store, realized_pnl=500.0)  # equity = 100500, above opening, well above peak - X

    await watcher.check_now(
        now=_MARKET_OPEN_NOW, token_store=token_store, ledger_store=store, lot_tracker=tracker,
        order_engine_order_service=order_service, notification_service=notifications, exit_all_lock=lock,
    )

    assert upstox.place_order_calls == []
    epoch = store.get_max_loss_epoch()
    assert epoch["peak_equity"] == 100500.0  # ratcheted up


@pytest.mark.anyio
async def test_check_now_flattens_and_re_arms_on_a_genuine_breach(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(
        tmp_path, held_quantities={"NSE_FO|1": 50},
    )
    store.upsert_max_loss_epoch(opening_balance=100000.0, peak_equity=100000.0, threshold_x=1000.0, epoch_started_at="2026-07-21T00:00:00+00:00")
    # realized_pnl -2000 -> equity = 98000, opening - X = 99000 -> breached
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY", remaining_quantity=50, realized_pnl=-2000.0)

    await watcher.check_now(
        now=_MARKET_OPEN_NOW, token_store=token_store, ledger_store=store, lot_tracker=tracker,
        order_engine_order_service=order_service, notification_service=notifications, exit_all_lock=lock,
    )

    assert len(upstox.place_order_calls) == 1
    placed = upstox.place_order_calls[0]
    assert placed["instrument_key"] == "NSE_FO|1"
    assert placed["transaction_type"] == "SELL"  # opposite of the lot's own BUY
    assert placed["quantity"] == 50

    epoch = store.get_max_loss_epoch()
    assert epoch["opening_balance"] == 98000.0  # re-armed at post-flatten equity
    assert epoch["peak_equity"] == 98000.0

    assert len(notifications.records) == 1
    assert notifications.records[0]["severity"] == "critical"


@pytest.mark.anyio
async def test_check_now_does_not_double_exit_a_lot_whose_own_stop_loss_fired_at_the_same_moment(tmp_path) -> None:
    """The exact race the user asked about: a lot's own stop-loss fires (a resting order already
    PLACED at the broker) at the same moment a portfolio-wide max-loss breach is independently
    detected. check_now must flatten every *other* open lot but skip this one -- not place a
    second, competing exit order for it."""
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(
        tmp_path, held_quantities={"NSE_FO|OTHER": 20},
    )
    store.upsert_max_loss_epoch(opening_balance=100000.0, peak_equity=100000.0, threshold_x=1000.0, epoch_started_at="2026-07-21T00:00:00+00:00")

    # lot-sl: its own stop-loss already fired and is resting at the broker (PLACED).
    _open_lot(store, lot_id="lot-sl", instrument_key="NSE_FO|SL", remaining_quantity=50, realized_pnl=-1500.0)
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-sl", instrument_key="NSE_FO|SL",
        role="STOP_LOSS", state="PLACED", condition_op="BELOW", condition_value=90.0,
    )
    store.upsert_lot(
        lot_id="lot-sl", instrument_key="NSE_FO|SL", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50, realized_pnl=-1500.0,
        state="OPEN", stoploss_rule_id="rule-sl",
    )

    # lot-other: unrelated, no bracket in flight -- must still be flattened normally.
    _open_lot(store, lot_id="lot-other", instrument_key="NSE_FO|OTHER", remaining_quantity=20, realized_pnl=-500.0)

    await watcher.check_now(
        now=_MARKET_OPEN_NOW, token_store=token_store, ledger_store=store, lot_tracker=tracker,
        order_engine_order_service=order_service, notification_service=notifications, exit_all_lock=lock,
    )

    assert len(upstox.place_order_calls) == 1
    assert upstox.place_order_calls[0]["instrument_key"] == "NSE_FO|OTHER"


@pytest.mark.anyio
async def test_check_now_is_a_no_op_when_there_is_no_stored_token(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(tmp_path, has_token=False)
    store.upsert_max_loss_epoch(opening_balance=100000.0, peak_equity=100000.0, threshold_x=1000.0, epoch_started_at="2026-07-21T00:00:00+00:00")
    _open_lot(store, realized_pnl=-5000.0)

    await watcher.check_now(
        now=_MARKET_OPEN_NOW, token_store=token_store, ledger_store=store, lot_tracker=tracker,
        order_engine_order_service=order_service, notification_service=notifications, exit_all_lock=lock,
    )

    assert upstox.place_order_calls == []


@pytest.mark.anyio
async def test_check_now_breaches_correctly_in_percentage_mode(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(
        tmp_path, held_quantities={"NSE_FO|1": 50},
    )
    # threshold_mode=PERCENTAGE, threshold_x=5 -> floor = openingBalance - 5% of openingBalance
    # = 100000 - 5000 = 95000. realized_pnl -6000 -> equity 94000, which breaches.
    store.upsert_max_loss_epoch(
        opening_balance=100000.0, peak_equity=100000.0, threshold_x=5.0,
        threshold_mode="PERCENTAGE", epoch_started_at="2026-07-21T00:00:00+00:00",
    )
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY", remaining_quantity=50, realized_pnl=-6000.0)

    await watcher.check_now(
        now=_MARKET_OPEN_NOW, token_store=token_store, ledger_store=store, lot_tracker=tracker,
        order_engine_order_service=order_service, notification_service=notifications, exit_all_lock=lock,
    )

    assert len(upstox.place_order_calls) == 1
    epoch = store.get_max_loss_epoch()
    # Auto re-arm carries the same threshold_mode/threshold_x pair forward unchanged -- still 5%,
    # now of the new (lower) opening balance.
    assert epoch["threshold_mode"] == "PERCENTAGE"
    assert epoch["threshold_x"] == 5.0
    assert epoch["opening_balance"] == 94000.0


@pytest.mark.anyio
async def test_check_now_does_not_breach_in_percentage_mode_within_the_cushion(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(tmp_path)
    # floor = 100000 - 5% of 100000 = 95000. realized_pnl -4000 -> equity 96000, still above floor.
    store.upsert_max_loss_epoch(
        opening_balance=100000.0, peak_equity=100000.0, threshold_x=5.0,
        threshold_mode="PERCENTAGE", epoch_started_at="2026-07-21T00:00:00+00:00",
    )
    _open_lot(store, realized_pnl=-4000.0)

    await watcher.check_now(
        now=_MARKET_OPEN_NOW, token_store=token_store, ledger_store=store, lot_tracker=tracker,
        order_engine_order_service=order_service, notification_service=notifications, exit_all_lock=lock,
    )

    assert upstox.place_order_calls == []


@pytest.mark.anyio
async def test_flatten_open_lots_skips_a_lot_whose_stop_loss_already_has_an_exit_placed(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(tmp_path)
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", remaining_quantity=50)
    # This lot's own stop-loss already fired and has a resting order at the broker (PLACED) --
    # flattening it again would place a second, independent exit for the same quantity.
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="PLACED", condition_op="BELOW", condition_value=90.0,
    )
    store.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50, realized_pnl=0.0,
        state="OPEN", stoploss_rule_id="rule-sl",
    )

    placed_lot_ids = await watcher.flatten_open_lots("token", store, order_service)

    assert placed_lot_ids == []
    assert upstox.place_order_calls == []


@pytest.mark.anyio
async def test_flatten_open_lots_still_flattens_a_lot_whose_bracket_is_only_armed_not_firing(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(
        tmp_path, held_quantities={"NSE_FO|1": 50},
    )
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", remaining_quantity=50)
    # ARMED, not yet fired -- nothing in flight yet, so the flatten must still protect this lot.
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=90.0,
    )
    store.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50, realized_pnl=0.0,
        state="OPEN", stoploss_rule_id="rule-sl",
    )

    placed_lot_ids = await watcher.flatten_open_lots("token", store, order_service)

    assert placed_lot_ids == ["lot-1"]
    assert len(upstox.place_order_calls) == 1


@pytest.mark.anyio
async def test_flatten_open_lots_still_flattens_a_lot_whose_bracket_leg_already_failed(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(
        tmp_path, held_quantities={"NSE_FO|1": 50},
    )
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", remaining_quantity=50)
    # A prior fire attempt failed -- terminal, nothing actually resting at the broker, so the lot
    # is still genuinely unprotected and the flatten must act.
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="FAILED", condition_op="BELOW", condition_value=90.0,
    )
    store.upsert_lot(
        lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY",
        entry_price=100.0, entry_quantity=50, remaining_quantity=50, realized_pnl=0.0,
        state="OPEN", stoploss_rule_id="rule-sl",
    )

    placed_lot_ids = await watcher.flatten_open_lots("token", store, order_service)

    assert placed_lot_ids == ["lot-1"]


@pytest.mark.anyio
async def test_flatten_open_lots_continues_past_one_lots_placement_failure(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(
        tmp_path, fail_instrument_keys={"NSE_FO|BAD"},
        held_quantities={"NSE_FO|BAD": 10, "NSE_FO|GOOD": 20},
    )
    _open_lot(store, lot_id="lot-bad", instrument_key="NSE_FO|BAD", remaining_quantity=10)
    _open_lot(store, lot_id="lot-good", instrument_key="NSE_FO|GOOD", remaining_quantity=20)

    placed_lot_ids = await watcher.flatten_open_lots("token", store, order_service)

    assert placed_lot_ids == ["lot-good"]
    assert len(upstox.place_order_calls) == 1
    assert upstox.place_order_calls[0]["instrument_key"] == "NSE_FO|GOOD"


# -- broker-truth quantity cap: the flatten's second line of defense against the same double-exit
# race the in-flight check above addresses via a different mechanism (a stale ledger, rather than
# an in-flight bracket leg the ledger already knows about).


@pytest.mark.anyio
async def test_flatten_open_lots_caps_the_exit_at_the_broker_reported_closeable_quantity(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(
        tmp_path, held_quantities={"NSE_FO|1": 30},  # broker says only 30 -- ledger says 50 (stale)
    )
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", remaining_quantity=50)

    placed_lot_ids = await watcher.flatten_open_lots("token", store, order_service)

    assert placed_lot_ids == ["lot-1"]
    assert len(upstox.place_order_calls) == 1
    assert upstox.place_order_calls[0]["quantity"] == 30  # capped, never the stale ledger's 50


@pytest.mark.anyio
async def test_flatten_open_lots_skips_a_lot_with_nothing_closeable_at_the_broker_at_all(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(
        tmp_path, held_quantities={},  # broker reports nothing held for this instrument at all
    )
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", remaining_quantity=50)

    placed_lot_ids = await watcher.flatten_open_lots("token", store, order_service)

    assert placed_lot_ids == []
    assert upstox.place_order_calls == []


@pytest.mark.anyio
async def test_flatten_open_lots_caps_a_short_lots_buy_to_cover_at_the_broker_reported_short_magnitude(tmp_path) -> None:
    # A short lot's entry was SELL; its close is BUY-to-cover. Broker reports a -20 (short 20)
    # position -- even though the ledger says remaining_quantity=50.
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(
        tmp_path, held_quantities={"NSE_FO|1": -20.0},
    )
    _open_lot(store, lot_id="lot-short", instrument_key="NSE_FO|1", transaction_type="SELL", remaining_quantity=50)

    placed_lot_ids = await watcher.flatten_open_lots("token", store, order_service)

    assert placed_lot_ids == ["lot-short"]
    placed = upstox.place_order_calls[0]
    assert placed["transaction_type"] == "BUY"
    assert placed["quantity"] == 20


@pytest.mark.anyio
async def test_flatten_open_lots_does_not_cap_when_broker_reported_quantity_is_generous(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications, lock = _watcher_deps(
        tmp_path, held_quantities={"NSE_FO|1": 500},  # far more than the lot's remaining_quantity
    )
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", remaining_quantity=50)

    placed_lot_ids = await watcher.flatten_open_lots("token", store, order_service)

    assert placed_lot_ids == ["lot-1"]
    assert upstox.place_order_calls[0]["quantity"] == 50  # never more than the ledger's own qty either

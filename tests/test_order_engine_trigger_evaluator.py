from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.services import order_engine_trigger_evaluator as evaluator
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
    def __init__(self, fail_instrument_keys: set[str] | None = None) -> None:
        self.place_order_calls: list[dict] = []
        self._fail_instrument_keys = fail_instrument_keys or set()

    async def get_order_book(self, access_token):
        return {"status": "success", "data": []}

    async def place_order(self, access_token, **kwargs):
        if kwargs.get("instrument_key") in self._fail_instrument_keys:
            raise RuntimeError("simulated broker failure")
        self.place_order_calls.append(kwargs)
        return {"status": "success", "data": {"order_id": f"order-{len(self.place_order_calls)}"}}


def _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY", entry_price=100.0, remaining_quantity=50, product="I"):
    store.upsert_lot(
        lot_id=lot_id, instrument_key=instrument_key, transaction_type=transaction_type,
        entry_price=entry_price, entry_quantity=remaining_quantity, remaining_quantity=remaining_quantity,
        realized_pnl=0.0, state="OPEN", product=product,
    )


def _evaluator_deps(tmp_path, *, fail_instrument_keys=None, has_token=True):
    store = OrderEngineLedgerStore(_settings(tmp_path))
    tracker = OrderEngineLotTracker(store)
    upstox = _FakeUpstox(fail_instrument_keys=fail_instrument_keys)
    order_service = OrderEngineOrderService(upstox)
    token_store = _FakeTokenStore(has_token=has_token)
    notifications = _FakeNotificationService()
    return store, tracker, upstox, order_service, token_store, notifications


@pytest.mark.anyio
async def test_check_now_is_a_no_op_when_market_is_closed(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications = _evaluator_deps(tmp_path)
    _open_lot(store)
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=95.0,
    )

    await evaluator.check_now(
        "NSE_FO|1", 90.0, now=_MARKET_CLOSED_NOW, token_store=token_store,
        ledger_store=store, order_engine_order_service=order_service, notification_service=notifications,
    )

    assert upstox.place_order_calls == []
    assert store.get_trigger_rule("rule-sl")["state"] == "ARMED"


@pytest.mark.anyio
async def test_check_now_is_a_no_op_with_no_ltp() -> None:
    # No DB needed at all -- ltp=None must short-circuit before touching the ledger.
    async def _boom(*args, **kwargs):
        raise AssertionError("should never be called")

    await evaluator.check_now(
        "NSE_FO|1", None, now=_MARKET_OPEN_NOW, token_store=_FakeTokenStore(),
        ledger_store=None, order_engine_order_service=None, notification_service=None,  # type: ignore[arg-type]
    )


@pytest.mark.anyio
async def test_check_now_fires_a_stop_loss_below_and_cancels_its_sibling_target(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications = _evaluator_deps(tmp_path)
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY", remaining_quantity=50)
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=95.0,
        sibling_rule_id="rule-tp",
    )
    store.upsert_trigger_rule(
        rule_id="rule-tp", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="TARGET", state="ARMED", condition_op="ABOVE", condition_value=120.0,
        sibling_rule_id="rule-sl",
    )

    await evaluator.check_now(
        "NSE_FO|1", 94.0, now=_MARKET_OPEN_NOW, token_store=token_store,
        ledger_store=store, order_engine_order_service=order_service, notification_service=notifications,
    )

    assert len(upstox.place_order_calls) == 1
    placed = upstox.place_order_calls[0]
    assert placed["transaction_type"] == "SELL"  # opposite of the lot's own BUY
    assert placed["quantity"] == 50
    assert placed["order_type"] == "MARKET"

    sl_rule = store.get_trigger_rule("rule-sl")
    assert sl_rule["state"] == "PLACED"
    assert sl_rule["version"] == 2  # ARMED(0) -> FIRING(1) -> PLACED(2)

    tp_rule = store.get_trigger_rule("rule-tp")
    assert tp_rule["state"] == "CANCELLED"  # OCO -- never placed at the broker, just cancelled locally


@pytest.mark.anyio
async def test_check_now_only_fires_the_stop_loss_when_both_legs_match_the_same_tick(tmp_path) -> None:
    """A gap candle crossing both bracket sides at once must resolve to the pessimistic outcome --
    the stop-loss fires, the target is cancelled, never the reverse and never both firing."""
    store, tracker, upstox, order_service, token_store, notifications = _evaluator_deps(tmp_path)
    _open_lot(store, lot_id="lot-1", instrument_key="NSE_FO|1", transaction_type="BUY", remaining_quantity=50)
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=95.0,
        sibling_rule_id="rule-tp",
    )
    store.upsert_trigger_rule(
        rule_id="rule-tp", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="TARGET", state="ARMED", condition_op="ABOVE", condition_value=90.0,  # already crossed too
        sibling_rule_id="rule-sl",
    )

    await evaluator.check_now(
        "NSE_FO|1", 92.0, now=_MARKET_OPEN_NOW, token_store=token_store,
        ledger_store=store, order_engine_order_service=order_service, notification_service=notifications,
    )

    assert len(upstox.place_order_calls) == 1  # only one leg actually fires
    assert store.get_trigger_rule("rule-sl")["state"] == "PLACED"
    assert store.get_trigger_rule("rule-tp")["state"] == "CANCELLED"


@pytest.mark.anyio
async def test_check_now_does_not_fire_a_rule_whose_condition_is_not_met(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications = _evaluator_deps(tmp_path)
    _open_lot(store)
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=95.0,
    )

    await evaluator.check_now(
        "NSE_FO|1", 100.0, now=_MARKET_OPEN_NOW, token_store=token_store,
        ledger_store=store, order_engine_order_service=order_service, notification_service=notifications,
    )

    assert upstox.place_order_calls == []
    assert store.get_trigger_rule("rule-sl")["state"] == "ARMED"


@pytest.mark.anyio
async def test_check_now_is_a_no_op_when_there_is_no_stored_token(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications = _evaluator_deps(tmp_path, has_token=False)
    _open_lot(store)
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=95.0,
    )

    await evaluator.check_now(
        "NSE_FO|1", 90.0, now=_MARKET_OPEN_NOW, token_store=token_store,
        ledger_store=store, order_engine_order_service=order_service, notification_service=notifications,
    )

    assert upstox.place_order_calls == []
    assert store.get_trigger_rule("rule-sl")["state"] == "ARMED"  # can't fire without a token


@pytest.mark.anyio
async def test_check_now_marks_failed_and_notifies_when_placement_raises(tmp_path) -> None:
    store, tracker, upstox, order_service, token_store, notifications = _evaluator_deps(
        tmp_path, fail_instrument_keys={"NSE_FO|1"},
    )
    _open_lot(store)
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=95.0,
    )

    await evaluator.check_now(
        "NSE_FO|1", 90.0, now=_MARKET_OPEN_NOW, token_store=token_store,
        ledger_store=store, order_engine_order_service=order_service, notification_service=notifications,
    )

    rule = store.get_trigger_rule("rule-sl")
    assert rule["state"] == "FAILED"
    assert rule["version"] == 2  # ARMED(0) -> FIRING(1) -> FAILED(2)
    assert len(notifications.records) == 1
    assert notifications.records[0]["severity"] == "critical"


@pytest.mark.anyio
async def test_concurrent_checks_for_the_same_instrument_never_double_fire_the_same_rule(tmp_path) -> None:
    """The exact race this module's own CAS exists for: two ticks for the same instrument racing
    check_now concurrently must still only fire a matching rule once."""
    store, tracker, upstox, order_service, token_store, notifications = _evaluator_deps(tmp_path)
    _open_lot(store)
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=95.0,
    )

    await asyncio.gather(*[
        evaluator.check_now(
            "NSE_FO|1", 90.0, now=_MARKET_OPEN_NOW, token_store=token_store,
            ledger_store=store, order_engine_order_service=order_service, notification_service=notifications,
        )
        for _ in range(20)
    ])

    assert len(upstox.place_order_calls) == 1
    assert store.get_trigger_rule("rule-sl")["state"] == "PLACED"


@pytest.mark.anyio
async def test_check_now_never_evaluates_a_crosses_condition(tmp_path) -> None:
    """v1 scope note, proven: CROSSES is not yet implemented server-side -- a rule using it must
    never fire from this module, not silently misinterpreted as ABOVE/BELOW."""
    store, tracker, upstox, order_service, token_store, notifications = _evaluator_deps(tmp_path)
    _open_lot(store)
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="CROSSES", condition_value=95.0,
    )

    await evaluator.check_now(
        "NSE_FO|1", 50.0, now=_MARKET_OPEN_NOW, token_store=token_store,
        ledger_store=store, order_engine_order_service=order_service, notification_service=notifications,
    )

    assert upstox.place_order_calls == []
    assert store.get_trigger_rule("rule-sl")["state"] == "ARMED"


@pytest.mark.anyio
async def test_run_fallback_loop_evaluates_every_instrument_with_an_open_lot_using_last_known_price(tmp_path) -> None:
    """[run_fallback_loop] itself hardcodes the real wall clock (`datetime.now(_IST)`) for its own
    market-open gate, same as `order_engine_max_loss_watcher.run_fallback_loop` -- neither is
    unit-tested against real time for exactly that reason (it'd be flaky outside market hours,
    passing or failing purely based on when the test happens to run). This test instead proves
    [run_fallback_loop]'s only real logic -- iterating [ledger_store_instrument_keys] and pulling
    each one's price from [OrderEngineLotTracker.last_ltp] rather than forcing a fresh quote -- by
    exercising that helper and [check_now] directly with an injected [now], the same pattern
    [check_now]'s own tests already use."""
    store, tracker, upstox, order_service, token_store, notifications = _evaluator_deps(tmp_path)
    _open_lot(store)
    store.upsert_trigger_rule(
        rule_id="rule-sl", lot_id="lot-1", instrument_key="NSE_FO|1",
        role="STOP_LOSS", state="ARMED", condition_op="BELOW", condition_value=95.0,
    )
    tracker.apply_tick("NSE_FO|1", 90.0)  # seeds last_ltp without going through check_now directly

    assert evaluator.ledger_store_instrument_keys(store) == {"NSE_FO|1"}

    await evaluator.check_now(
        "NSE_FO|1", tracker.last_ltp("NSE_FO|1"), now=_MARKET_OPEN_NOW, token_store=token_store,
        ledger_store=store, order_engine_order_service=order_service, notification_service=notifications,
    )

    assert store.get_trigger_rule("rule-sl")["state"] == "PLACED"
    assert len(upstox.place_order_calls) == 1

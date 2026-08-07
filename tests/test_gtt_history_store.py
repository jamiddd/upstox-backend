from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.services.gtt_history_store import GttHistoryStore


def _settings(path: Path) -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/unused.enc"),
        gtt_database_path=path,
    )


def test_list_returns_nothing_when_nothing_archived_yet(tmp_path: Path) -> None:
    store = GttHistoryStore(_settings(tmp_path / "gtt.sqlite3"))

    assert store.list() == []


def test_archive_and_list_round_trip(tmp_path: Path) -> None:
    store = GttHistoryStore(_settings(tmp_path / "gtt.sqlite3"))
    order = {"gtt_order_id": "GTT-1", "instrument_token": "NSE_FO|111", "status": "ACTIVE"}

    store.archive([order])

    assert store.list() == [order]
    assert store.list("NSE_FO|111") == [order]
    assert store.list("NSE_FO|999") == []


def test_archive_overwrites_stale_status_on_a_later_call(tmp_path: Path) -> None:
    """A status transition (e.g. a bracket cancelled once its position is flattened, see
    SmartOrderService._cancel_stray_gtts) must replace the archived row, not leave the old
    ACTIVE status behind next to it."""
    store = GttHistoryStore(_settings(tmp_path / "gtt.sqlite3"))
    active = {"gtt_order_id": "GTT-1", "instrument_token": "NSE_FO|111", "status": "ACTIVE"}
    cancelled = {"gtt_order_id": "GTT-1", "instrument_token": "NSE_FO|111", "status": "CANCELLED"}

    store.archive([active])
    store.archive([cancelled])

    assert store.list() == [cancelled]


def test_archive_skips_orders_without_a_stable_id(tmp_path: Path) -> None:
    store = GttHistoryStore(_settings(tmp_path / "gtt.sqlite3"))

    store.archive([{"instrument_token": "NSE_FO|111", "status": "ACTIVE"}])

    assert store.list() == []


def test_archive_retains_an_order_even_once_a_later_call_omits_it(tmp_path: Path) -> None:
    """Durability is the point: an order that Upstox itself has stopped listing (e.g. aged out of
    its own API) must still be recoverable from the archive, not silently dropped."""
    store = GttHistoryStore(_settings(tmp_path / "gtt.sqlite3"))
    kept = {"gtt_order_id": "GTT-1", "instrument_token": "NSE_FO|111", "status": "COMPLETED"}
    other = {"gtt_order_id": "GTT-2", "instrument_token": "NSE_FO|222", "status": "ACTIVE"}

    store.archive([kept, other])
    store.archive([other])  # A later poll no longer reports GTT-1 at all.

    assert {order["gtt_order_id"] for order in store.list()} == {"GTT-1", "GTT-2"}


def test_record_placed_is_durable_with_no_prior_archive_call(tmp_path: Path) -> None:
    """The actual fix: a placed order is known here the moment it's recorded, independent of
    archive()/a live Upstox list ever having run at all."""
    store = GttHistoryStore(_settings(tmp_path / "gtt.sqlite3"))

    store.record_placed("GTT-1", "NSE_FO|111", {"gtt_order_id": "GTT-1", "quantity": 75})

    rows = store.list("NSE_FO|111")
    assert len(rows) == 1
    assert rows[0]["gtt_order_id"] == "GTT-1"
    assert rows[0]["status"] == "ACTIVE"
    assert rows[0]["quantity"] == 75


def test_record_modified_overwrites_the_stored_payload(tmp_path: Path) -> None:
    store = GttHistoryStore(_settings(tmp_path / "gtt.sqlite3"))
    store.record_placed("GTT-1", "NSE_FO|111", {"gtt_order_id": "GTT-1", "quantity": 75})

    store.record_modified("GTT-1", "NSE_FO|111", {"gtt_order_id": "GTT-1", "quantity": 150})

    rows = store.list("NSE_FO|111")
    assert len(rows) == 1
    assert rows[0]["quantity"] == 150


def test_record_cancelled_marks_a_known_order_cancelled(tmp_path: Path) -> None:
    store = GttHistoryStore(_settings(tmp_path / "gtt.sqlite3"))
    store.record_placed("GTT-1", "NSE_FO|111", {"gtt_order_id": "GTT-1", "status": "ACTIVE"})

    store.record_cancelled("GTT-1")

    rows = store.list("NSE_FO|111")
    assert rows[0]["status"] == "CANCELLED"


def test_record_cancelled_is_a_no_op_for_an_unknown_id(tmp_path: Path) -> None:
    """A cancel for an id this backend never recorded (e.g. a GTT placed before this backend
    tracked its own placements) must not fabricate a row out of nothing."""
    store = GttHistoryStore(_settings(tmp_path / "gtt.sqlite3"))

    store.record_cancelled("GTT-never-seen")

    assert store.list() == []

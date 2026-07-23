from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.services.pending_oco_pairs_store import PendingExit, PendingOcoPairsStore


def _settings(path: Path) -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/unused.enc"),
        pending_oco_pairs_path=path,
    )


def _pending_exit(
    stoploss_order_id: str = "S-1",
    instrument_key: str = "NSE_FO|111",
    exit_transaction_type: str = "SELL",
    quantity: int = 75,
    product: str = "I",
    target_trigger_price: float = 140.0,
) -> PendingExit:
    return PendingExit(
        stoploss_order_id=stoploss_order_id,
        instrument_key=instrument_key,
        exit_transaction_type=exit_transaction_type,
        quantity=quantity,
        product=product,
        target_trigger_price=target_trigger_price,
    )


def test_load_returns_empty_list_when_nothing_saved_yet(tmp_path: Path) -> None:
    store = PendingOcoPairsStore(_settings(tmp_path / "pairs.json"))

    assert store.load() == []


def test_add_and_load_round_trip(tmp_path: Path) -> None:
    store = PendingOcoPairsStore(_settings(tmp_path / "pairs.json"))
    pending_exit = _pending_exit()

    store.add(pending_exit)

    assert store.load() == [pending_exit]


def test_add_appends_to_existing_pending_exits(tmp_path: Path) -> None:
    store = PendingOcoPairsStore(_settings(tmp_path / "pairs.json"))
    first = _pending_exit(stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    second = _pending_exit(stoploss_order_id="S-2", instrument_key="NSE_FO|222")

    store.add(first)
    store.add(second)

    assert store.load() == [first, second]


def test_remove_drops_only_the_resolved_pending_exits(tmp_path: Path) -> None:
    store = PendingOcoPairsStore(_settings(tmp_path / "pairs.json"))
    keep = _pending_exit(stoploss_order_id="S-1")
    drop = _pending_exit(stoploss_order_id="S-2")
    store.add(keep)
    store.add(drop)

    store.remove([drop])

    assert store.load() == [keep]


def test_remove_ignores_a_pending_exit_not_actually_present(tmp_path: Path) -> None:
    store = PendingOcoPairsStore(_settings(tmp_path / "pairs.json"))
    keep = _pending_exit(stoploss_order_id="S-1")
    store.add(keep)
    not_present = _pending_exit(stoploss_order_id="S-x")

    store.remove([not_present])

    assert store.load() == [keep]


def test_load_tolerates_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "pairs.json"
    path.write_text("not valid json", encoding="utf-8")
    store = PendingOcoPairsStore(_settings(path))

    assert store.load() == []


def test_load_tolerates_unexpected_json_shape(tmp_path: Path) -> None:
    path = tmp_path / "pairs.json"
    path.write_text('{"pairs": "not-a-list"}', encoding="utf-8")
    store = PendingOcoPairsStore(_settings(path))

    assert store.load() == []


def test_load_skips_malformed_entries(tmp_path: Path) -> None:
    path = tmp_path / "pairs.json"
    path.write_text(
        '{"pairs": [{"stoploss_order_id": "S-1"}, '
        '{"stoploss_order_id": "S-2", "instrument_key": "NSE_FO|222", '
        '"exit_transaction_type": "SELL", "quantity": 75, "product": "I", '
        '"target_trigger_price": 140.0}]}',
        encoding="utf-8",
    )
    store = PendingOcoPairsStore(_settings(path))

    assert store.load() == [
        PendingExit(
            stoploss_order_id="S-2",
            instrument_key="NSE_FO|222",
            exit_transaction_type="SELL",
            quantity=75,
            product="I",
            target_trigger_price=140.0,
        ),
    ]

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.services.pending_oco_pairs_store import OcoPair, PendingOcoPairsStore


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


def test_load_returns_empty_list_when_nothing_saved_yet(tmp_path: Path) -> None:
    store = PendingOcoPairsStore(_settings(tmp_path / "pairs.json"))

    assert store.load() == []


def test_add_and_load_round_trip(tmp_path: Path) -> None:
    store = PendingOcoPairsStore(_settings(tmp_path / "pairs.json"))
    pair = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")

    store.add(pair)

    assert store.load() == [pair]


def test_add_appends_to_existing_pairs(tmp_path: Path) -> None:
    store = PendingOcoPairsStore(_settings(tmp_path / "pairs.json"))
    first = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    second = OcoPair(target_order_id="T-2", stoploss_order_id="S-2", instrument_key="NSE_FO|222")

    store.add(first)
    store.add(second)

    assert store.load() == [first, second]


def test_remove_drops_only_the_resolved_pairs(tmp_path: Path) -> None:
    store = PendingOcoPairsStore(_settings(tmp_path / "pairs.json"))
    keep = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    drop = OcoPair(target_order_id="T-2", stoploss_order_id="S-2", instrument_key="NSE_FO|222")
    store.add(keep)
    store.add(drop)

    store.remove([drop])

    assert store.load() == [keep]


def test_remove_ignores_a_pair_not_actually_present(tmp_path: Path) -> None:
    store = PendingOcoPairsStore(_settings(tmp_path / "pairs.json"))
    keep = OcoPair(target_order_id="T-1", stoploss_order_id="S-1", instrument_key="NSE_FO|111")
    store.add(keep)
    not_present = OcoPair(target_order_id="T-x", stoploss_order_id="S-x", instrument_key="NSE_FO|999")

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
        '{"pairs": [{"target_order_id": "T-1"}, '
        '{"target_order_id": "T-2", "stoploss_order_id": "S-2", "instrument_key": "NSE_FO|222"}]}',
        encoding="utf-8",
    )
    store = PendingOcoPairsStore(_settings(path))

    assert store.load() == [
        OcoPair(target_order_id="T-2", stoploss_order_id="S-2", instrument_key="NSE_FO|222"),
    ]

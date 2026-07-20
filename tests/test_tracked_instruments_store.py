from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.services.tracked_instruments_store import TrackedInstrumentsStore


def _settings(path: Path) -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/unused.enc"),
        tracked_instruments_path=path,
    )


def test_load_returns_empty_list_when_nothing_saved_yet(tmp_path: Path) -> None:
    store = TrackedInstrumentsStore(_settings(tmp_path / "tracked.json"))

    assert store.load() == []


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    store = TrackedInstrumentsStore(_settings(tmp_path / "tracked.json"))

    store.save(["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"])

    assert store.load() == ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"]


def test_save_replaces_the_whole_set_not_merges(tmp_path: Path) -> None:
    store = TrackedInstrumentsStore(_settings(tmp_path / "tracked.json"))
    store.save(["NSE_INDEX|Nifty 50"])

    store.save(["BSE_INDEX|SENSEX"])

    assert store.load() == ["BSE_INDEX|SENSEX"]


def test_save_deduplicates_and_drops_blank_keys(tmp_path: Path) -> None:
    store = TrackedInstrumentsStore(_settings(tmp_path / "tracked.json"))

    store.save(["NSE_INDEX|Nifty 50", "", "NSE_INDEX|Nifty 50"])

    assert store.load() == ["NSE_INDEX|Nifty 50"]


def test_load_tolerates_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "tracked.json"
    path.write_text("not valid json", encoding="utf-8")
    store = TrackedInstrumentsStore(_settings(path))

    assert store.load() == []


def test_load_tolerates_unexpected_json_shape(tmp_path: Path) -> None:
    path = tmp_path / "tracked.json"
    path.write_text('{"underlying_keys": "not-a-list"}', encoding="utf-8")
    store = TrackedInstrumentsStore(_settings(path))

    assert store.load() == []

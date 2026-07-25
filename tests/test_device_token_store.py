from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.device_token_store import DeviceTokenStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="k",
        upstox_api_secret="s",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=tmp_path / "token.enc",
        device_token_path=tmp_path / "device_token.json",
    )


def test_load_defaults_when_nothing_saved_yet(tmp_path: Path) -> None:
    store = DeviceTokenStore(_settings(tmp_path))

    token, preference = store.load()

    assert token is None
    assert preference == "critical"


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = DeviceTokenStore(_settings(tmp_path))

    store.save(fcm_token="abc123", push_preference="everything")
    token, preference = store.load()

    assert token == "abc123"
    assert preference == "everything"


def test_save_replaces_previous_state_entirely(tmp_path: Path) -> None:
    store = DeviceTokenStore(_settings(tmp_path))
    store.save(fcm_token="old-token", push_preference="everything")

    store.save(fcm_token="new-token", push_preference="off")
    token, preference = store.load()

    assert token == "new-token"
    assert preference == "off"


def test_save_rejects_unknown_preference(tmp_path: Path) -> None:
    store = DeviceTokenStore(_settings(tmp_path))

    with pytest.raises(ValueError):
        store.save(fcm_token="abc", push_preference="sometimes")


def test_load_degrades_gracefully_on_corrupt_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.device_token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.device_token_path.write_text("not json", encoding="utf-8")
    store = DeviceTokenStore(settings)

    token, preference = store.load()

    assert token is None
    assert preference == "critical"

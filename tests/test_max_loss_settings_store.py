from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.services.max_loss_settings_store import MaxLossSettingsStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=Path("/tmp/token.enc"),
        max_loss_settings_path=tmp_path / "max_loss_settings.json",
    )


def test_defaults_to_zero_when_no_file_exists(tmp_path: Path) -> None:
    store = MaxLossSettingsStore(_settings(tmp_path))

    assert store.load() == 0.0


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = MaxLossSettingsStore(_settings(tmp_path))

    store.save(2500.0)

    assert store.load() == 2500.0


def test_clear_resets_to_zero(tmp_path: Path) -> None:
    store = MaxLossSettingsStore(_settings(tmp_path))
    store.save(2500.0)

    store.clear()

    assert store.load() == 0.0


def test_corrupt_file_degrades_to_zero_rather_than_raising(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.max_loss_settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings.max_loss_settings_path.write_text("not valid json", encoding="utf-8")
    store = MaxLossSettingsStore(settings)

    assert store.load() == 0.0


def test_non_numeric_amount_degrades_to_zero(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.max_loss_settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings.max_loss_settings_path.write_text('{"amount": "not-a-number"}', encoding="utf-8")
    store = MaxLossSettingsStore(settings)

    assert store.load() == 0.0

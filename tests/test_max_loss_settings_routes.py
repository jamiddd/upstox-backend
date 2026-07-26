from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_max_loss_settings_store
from app.api.routes import router
from app.core.config import Settings, get_settings
from app.services.max_loss_settings_store import MaxLossSettingsStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="k",
        upstox_api_secret="s",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=tmp_path / "token.enc",
        max_loss_settings_path=tmp_path / "max_loss_settings.json",
    )


def _client(tmp_path: Path) -> tuple[TestClient, MaxLossSettingsStore]:
    settings = _settings(tmp_path)
    store = MaxLossSettingsStore(settings)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_max_loss_settings_store] = lambda: store
    return TestClient(app), store


_HEADERS = {"X-API-Key": "mobile-secret"}


def test_get_max_loss_settings_requires_api_key(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.get("/api/settings/max-loss")

    assert response.status_code == 401


def test_get_max_loss_settings_defaults_to_zero(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.get("/api/settings/max-loss", headers=_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"amount": 0.0}


def test_put_max_loss_settings_persists_and_get_reflects_it(tmp_path: Path) -> None:
    client, store = _client(tmp_path)

    put_response = client.put(
        "/api/settings/max-loss", headers=_HEADERS, json={"amount": 3000.0},
    )
    get_response = client.get("/api/settings/max-loss", headers=_HEADERS)

    assert put_response.status_code == 200
    assert put_response.json() == {"amount": 3000.0}
    assert get_response.json() == {"amount": 3000.0}
    assert store.load() == 3000.0


def test_put_max_loss_settings_rejects_negative_amount(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.put("/api/settings/max-loss", headers=_HEADERS, json={"amount": -1.0})

    assert response.status_code == 422


def test_put_max_loss_settings_zero_disables_it(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    store.save(5000.0)

    response = client.put("/api/settings/max-loss", headers=_HEADERS, json={"amount": 0.0})

    assert response.status_code == 200
    assert store.load() == 0.0

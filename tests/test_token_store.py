from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.exceptions import UpstoxAuthRequiredError
from app.services.token_store import EncryptedTokenStore


def _settings(token_path: Path, key: str) -> Settings:
    return Settings(
        upstox_api_key="api-key",
        upstox_api_secret="api-secret",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key=key,
        token_store_path=token_path,
    )


def test_encrypted_token_store_round_trip(tmp_path: Path) -> None:
    """Save, decrypt, load, and clear an Upstox token payload."""
    key = Fernet.generate_key().decode("utf-8")
    token_path = tmp_path / "upstox_token.enc"
    store = EncryptedTokenStore(_settings(token_path, key))

    store.save({"access_token": "upstox-token", "user_id": "user-1"})

    assert store.has_token() is True
    assert b"upstox-token" not in token_path.read_bytes()
    assert store.load_access_token() == "upstox-token"

    store.clear()
    assert store.has_token() is False


def test_encrypted_token_store_requires_existing_token(tmp_path: Path) -> None:
    """Missing token files are reported as an auth-required condition."""
    key = Fernet.generate_key().decode("utf-8")
    store = EncryptedTokenStore(_settings(tmp_path / "missing.enc", key))

    with pytest.raises(UpstoxAuthRequiredError):
        store.load_access_token()

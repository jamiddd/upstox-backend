from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.fcm_service import FcmService

# Not a real key -- firebase_admin.credentials.Certificate only checks that this shape parses;
# it never actually contacts Google until a message is sent, which these tests stub out anyway.
_FAKE_SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "personalscalper-test",
    "private_key_id": "test-key-id",
    "private_key": (
        "-----BEGIN PRIVATE KEY-----\n"
        "MC4CAQAwBQYDK2VwBCIEIKp1s4tYA1yjwF9UKGjTe1sSNlkVR/dcAaJnhwuqQ+DE\n"
        "-----END PRIVATE KEY-----\n"
    ),
    "client_email": "test@personalscalper-test.iam.gserviceaccount.com",
    "client_id": "123456789",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _settings(tmp_path: Path, *, service_account_path: Path) -> Settings:
    return Settings(
        upstox_api_key="k",
        upstox_api_secret="s",
        upstox_redirect_url="https://example.com/api/auth/callback",
        upstox_environment="sandbox",
        mobile_api_key="mobile-secret",
        token_encryption_key="",
        token_store_path=tmp_path / "token.enc",
        notification_database_path=tmp_path / "notifications.sqlite3",
        device_token_path=tmp_path / "device_token.json",
        firebase_service_account_path=service_account_path,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_send_is_a_noop_when_unconfigured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, service_account_path=tmp_path / "missing.json")
    service = FcmService(settings)

    assert service._app is None
    # Must not raise even though no Firebase app was ever initialized.
    await service.send("some-token", title="Title", body="Body", data={"category": "system"})


@pytest.mark.anyio
async def test_send_builds_expected_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    account_path = tmp_path / "firebase_service_account.json"
    account_path.write_text(json.dumps(_FAKE_SERVICE_ACCOUNT), encoding="utf-8")
    settings = _settings(tmp_path, service_account_path=account_path)
    service = FcmService(settings)

    assert service._app is not None

    captured: dict[str, Any] = {}

    def _fake_send(message: Any, app: Any = None) -> str:
        captured["message"] = message
        captured["app"] = app
        return "projects/personalscalper-test/messages/fake-id"

    monkeypatch.setattr("app.services.fcm_service.messaging.send", _fake_send)

    await service.send(
        "device-token",
        title="Order filled",
        body="Order for NIFTY completed.",
        data={"notification_id": "42", "category": "orders"},
    )

    message = captured["message"]
    assert message.fid == "device-token"
    assert message.data == {
        "title": "Order filled",
        "body": "Order for NIFTY completed.",
        "notification_id": "42",
        "category": "orders",
    }
    assert message.notification is None
    assert captured["app"] is service._app

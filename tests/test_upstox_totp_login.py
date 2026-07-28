from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import anyio
import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import AppConfigError, UpstoxAutoLoginError
from app.services import upstox_totp_login
from app.services.upstox_service import UpstoxService
from app.services.upstox_totp_login import UpstoxTotpLoginService


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "upstox_api_key": "api-key",
        "upstox_api_secret": "api-secret",
        "upstox_redirect_url": "https://example.com/api/auth/callback",
        "upstox_environment": "sandbox",
        "mobile_api_key": "mobile-secret",
        "token_encryption_key": "",
        "token_store_path": Path("/tmp/token.enc"),
        "upstox_totp_username": "9999999999",
        "upstox_totp_secret": "JBSWY3DPEHPK3PXP",
        "upstox_totp_pin": "1234",
        "upstox_token_url": "https://api.test/v2/login/authorization/token",
    }
    values.update(overrides)
    return Settings(**values)


class _FakeResponse:
    def __init__(
        self,
        *,
        json_body: Optional[dict[str, Any]] = None,
        url: str = "",
        content_type: str = "application/json",
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._json_body = json_body
        self.url = url
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        assert self._json_body is not None
        return self._json_body


class _FakeSession:
    """Stands in for curl_cffi's Session -- calls are dispatched by URL substring, matching
    UpstoxTotpLoginService's own fixed step order rather than asserting exact call args, so the
    fake stays simple while still exercising the real step sequence."""

    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._dispatch(url)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._dispatch(url)

    def _dispatch(self, url: str) -> _FakeResponse:
        self.calls.append(url)
        for key, response in self._responses.items():
            if key in url:
                return response
        raise AssertionError(f"Unexpected request to {url}")


def _happy_path_responses() -> dict[str, _FakeResponse]:
    return {
        "authorization/dialog": _FakeResponse(
            url="https://login.upstox.com/login?user_id=U123&client_id=C456&user_type=individual",
            content_type="text/html",
        ),
        "otp/generate": _FakeResponse(
            json_body={"success": True, "data": {"validateOTPToken": "otp-token-1"}},
        ),
        "otp-totp/verify": _FakeResponse(
            json_body={"success": True, "data": {"isSecretPinSet": True}},
        ),
        "auth/2fa": _FakeResponse(
            json_body={"success": True, "data": {"userType": "individual"}},
        ),
        "oauth/authorize": _FakeResponse(
            json_body={
                "success": True,
                "data": {
                    "redirectUri": "https://example.com/api/auth/callback?code=auth-code-123",
                    "isApproved": True,
                },
            },
        ),
    }


def _upstox_service_returning(payload: dict[str, Any]) -> UpstoxService:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return UpstoxService(_settings(), client=client)


def test_login_runs_full_sequence_and_returns_token_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeSession(_happy_path_responses())
    monkeypatch.setattr(upstox_totp_login.curl_requests, "Session", lambda **kwargs: fake_session)

    upstox_service = _upstox_service_returning({"access_token": "final-token"})
    service = UpstoxTotpLoginService(_settings(), upstox_service)

    payload = anyio.run(service.login)

    assert payload == {"access_token": "final-token"}
    # Confirms the real step order ran, not just that *a* request happened at each URL.
    assert [
        "authorization/dialog" in fake_session.calls[0],
        "otp/generate" in fake_session.calls[1],
        "otp-totp/verify" in fake_session.calls[2],
        "auth/2fa" in fake_session.calls[3],
        "oauth/authorize" in fake_session.calls[4],
    ] == [True, True, True, True, True]


def test_login_raises_when_missing_totp_credentials() -> None:
    service = UpstoxTotpLoginService(
        _settings(upstox_totp_secret=""),
        _upstox_service_returning({"access_token": "unused"}),
    )

    with pytest.raises(AppConfigError):
        anyio.run(service.login)


def test_login_raises_upstox_auto_login_error_when_dialog_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _happy_path_responses()
    responses["authorization/dialog"] = _FakeResponse(
        json_body={"success": False, "error": {"message": "invalid client"}},
    )
    fake_session = _FakeSession(responses)
    monkeypatch.setattr(upstox_totp_login.curl_requests, "Session", lambda **kwargs: fake_session)

    service = UpstoxTotpLoginService(_settings(), _upstox_service_returning({"access_token": "unused"}))

    with pytest.raises(UpstoxAutoLoginError, match="authorization/dialog"):
        anyio.run(service.login)


def test_login_raises_upstox_auto_login_error_when_oauth_step_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _happy_path_responses()
    responses["oauth/authorize"] = _FakeResponse(
        json_body={"success": False, "error": {"message": "PIN mismatch"}},
    )
    fake_session = _FakeSession(responses)
    monkeypatch.setattr(upstox_totp_login.curl_requests, "Session", lambda **kwargs: fake_session)

    service = UpstoxTotpLoginService(_settings(), _upstox_service_returning({"access_token": "unused"}))

    with pytest.raises(UpstoxAutoLoginError, match="oauth/authorize"):
        anyio.run(service.login)

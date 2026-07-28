from __future__ import annotations

import asyncio
import base64
import logging
import random
import string
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import pyotp
from curl_cffi import requests as curl_requests

from app.core.config import Settings
from app.core.exceptions import UpstoxAutoLoginError
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

# Upstox's internal redirect for 2FA and OAuth approval. This is distinct from
# the application's registered redirect URL used by the initial dialog and token exchange.
_UPSTOX_INTERNAL_REDIRECT_URI = "https://api-v2.upstox.com/login/authorization/redirect"

_API_HOST = "https://api.upstox.com"
_SERVICE_HOST = "https://service.upstox.com"
_LOGIN_HOST = "https://login.upstox.com"


class UpstoxTotpLoginService:
    """Automate Upstox's TOTP, PIN, and OAuth authorization sequence.

    The login endpoints used here are undocumented and may change. The final token exchange is
    delegated to ``UpstoxService`` so token handling remains consistent with the browser flow.
    """

    def __init__(self, settings: Settings, upstox_service: UpstoxService) -> None:
        self._settings = settings
        self._upstox_service = upstox_service

    async def login(self) -> dict[str, Any]:
        """Complete login and return the token payload without persisting it."""
        self._settings.require_upstox_totp_login()
        code = await asyncio.to_thread(self._obtain_authorization_code)
        return await self._upstox_service.exchange_code_for_token(code)

    def _obtain_authorization_code(self) -> str:
        request_id = _generate_request_id()
        with curl_requests.Session(
            impersonate="chrome131",
            headers=_build_headers(request_id),
        ) as session:
            user_id, client_id = self._get_user_id_and_client_id(session)
            otp_token = self._generate_otp(session, user_id)
            self._verify_totp(session, otp_token)
            self._submit_pin(session, client_id)
            return self._authorize_oauth(session, client_id, request_id)

    def _get_user_id_and_client_id(
        self,
        session: curl_requests.Session,
    ) -> tuple[str, str]:
        """Start authorization and extract the identifiers from the login redirect."""
        response = session.get(
            f"{_API_HOST}/v2/login/authorization/dialog",
            params={
                "response_type": "code",
                "client_id": self._settings.upstox_api_key,
                "redirect_uri": self._settings.upstox_redirect_url,
            },
            allow_redirects=True,
        )
        if _is_json_response(response):
            raise UpstoxAutoLoginError(
                "authorization/dialog returned JSON instead of a login redirect: "
                f"{response.text[:500]}",
            )

        params = _redirect_params(response.url)
        return (
            _require_param(params, "user_id", step="authorization/dialog"),
            _require_param(params, "client_id", step="authorization/dialog"),
        )

    def _generate_otp(self, session: curl_requests.Session, user_id: str) -> str:
        """Register the login attempt and return the token used for TOTP verification."""
        data = _post_json(
            session,
            f"{_SERVICE_HOST}/login/open/v6/auth/1fa/otp/generate",
            step="otp/generate",
            json={
                "data": {
                    "mobileNumber": self._settings.upstox_totp_username,
                    "userId": user_id,
                },
            },
        )
        return _require_field(data, "validateOTPToken", step="otp/generate")

    def _verify_totp(
        self,
        session: curl_requests.Session,
        validate_otp_token: str,
    ) -> None:
        """Submit the current TOTP code."""
        _post_json(
            session,
            f"{_SERVICE_HOST}/login/open/v4/auth/1fa/otp-totp/verify",
            step="otp-totp/verify",
            json={
                "data": {
                    "otp": pyotp.TOTP(self._settings.upstox_totp_secret).now(),
                    "validateOtpToken": validate_otp_token,
                },
            },
        )

    def _submit_pin(self, session: curl_requests.Session, client_id: str) -> None:
        """Submit the base64-encoded transaction PIN as Upstox's SECRET_PIN method."""
        encoded_pin = base64.b64encode(
            self._settings.upstox_totp_pin.encode(),
        ).decode()
        _post_json(
            session,
            f"{_SERVICE_HOST}/login/open/v3/auth/2fa",
            step="2fa",
            params={
                "client_id": client_id,
                "redirect_uri": _UPSTOX_INTERNAL_REDIRECT_URI,
            },
            json={
                "data": {
                    "twoFAMethod": "SECRET_PIN",
                    "inputText": encoded_pin,
                },
            },
        )

    def _authorize_oauth(
        self,
        session: curl_requests.Session,
        client_id: str,
        request_id: str,
    ) -> str:
        """Approve OAuth consent and extract the authorization code."""
        data = _post_json(
            session,
            f"{_SERVICE_HOST}/login/v2/oauth/authorize",
            step="oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": _UPSTOX_INTERNAL_REDIRECT_URI,
                "requestId": request_id,
                "response_type": "code",
            },
            json={"data": {"userOAuthApproval": True}},
        )
        redirect_uri = _require_field(data, "redirectUri", step="oauth/authorize")
        return _require_param(
            _redirect_params(redirect_uri),
            "code",
            step="oauth/authorize",
        )


def _generate_request_id() -> str:
    characters = string.ascii_letters + string.digits
    return "WPRO-" + "".join(random.choices(characters, k=10))


def _build_headers(request_id: str) -> dict[str, str]:
    return {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": _LOGIN_HOST,
        "referer": _LOGIN_HOST,
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "x-request-id": request_id,
    }


def _post_json(
    session: curl_requests.Session,
    url: str,
    *,
    step: str,
    params: Optional[dict[str, str]] = None,
    json: dict[str, Any],
) -> dict[str, Any]:
    response = session.post(url, params=params, json=json)
    return _parse_json_response(response, step=step)


def _is_json_response(response: Any) -> bool:
    content_type = response.headers.get("Content-Type") or ""
    return "application/json" in content_type.lower()


def _parse_json_response(response: Any, *, step: str) -> dict[str, Any]:
    if not _is_json_response(response):
        raise UpstoxAutoLoginError(
            f"{step}: expected JSON, got HTTP {response.status_code}",
        )

    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise UpstoxAutoLoginError(f"{step}: invalid JSON response") from error

    if not isinstance(payload, dict):
        raise UpstoxAutoLoginError(
            f"{step}: unexpected response shape: {payload!r}",
        )

    data = payload.get("data")
    request_failed = not payload.get("success", True)
    data_failed = isinstance(data, dict) and data.get("status") == "error"
    if request_failed or data_failed:
        reason = payload.get("error") or data
        raise UpstoxAutoLoginError(
            f"{step}: Upstox rejected the request: {reason}",
        )

    return data if isinstance(data, dict) else payload


def _redirect_params(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(uri).query)


def _require_field(data: dict[str, Any], field: str, *, step: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise UpstoxAutoLoginError(
            f"{step}: response missing expected field '{field}': {data!r}",
        )
    return value


def _require_param(
    params: dict[str, list[str]],
    name: str,
    *,
    step: str,
) -> str:
    values = params.get(name)
    if not values:
        raise UpstoxAutoLoginError(
            f"{step}: missing '{name}' in redirect params: {params!r}",
        )
    return values[0]

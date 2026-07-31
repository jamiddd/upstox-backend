from __future__ import annotations

import base64
import hashlib
import hmac
import time

from app.core.config import Settings

# Name of the HttpOnly cookie the web client's session lives in -- see require_web_session in
# app/core/security.py for where it's read back, and stream_routes.py's stream_endpoint for the
# WebSocket-side equivalent (browsers send cookies automatically on the upgrade request, unlike
# custom headers, which they cannot set on a WS handshake at all).
WEB_SESSION_COOKIE_NAME = "psw_session"

# 24h -- the upper end of WEB_CLIENT_ROADMAP.md's "12-24h with silent refresh" range. Silent
# refresh-on-activity is not implemented yet (deferred past M0); a plain fixed expiry is enough to
# prove the cookie round-trips correctly across the CORS boundary.
DEFAULT_SESSION_TTL_SECONDS = 24 * 60 * 60


def create_session_token(settings: Settings, *, issued_at: float | None = None) -> str:
    """Mint a signed session token: base64(issued_at).hex(hmac_signature).

    This is a single-user tool with exactly one session type to issue, so a ~15-line HMAC+TTL
    scheme is used directly (stdlib `hmac`/`hashlib`) rather than pulling in a library like
    `itsdangerous` for the same amount of logic -- one fewer dependency to audit.
    """
    issued_at = time.time() if issued_at is None else issued_at
    payload = f"{issued_at:.6f}".encode("ascii")
    encoded_payload = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = _sign(settings, encoded_payload)
    return f"{encoded_payload}.{signature}"


def verify_session_token(
    token: str,
    settings: Settings,
    *,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    now: float | None = None,
) -> bool:
    """Return True if `token` was issued by this backend, is unexpired, and untampered."""
    if not token or token.count(".") != 1:
        return False
    encoded_payload, signature = token.split(".", 1)
    expected_signature = _sign(settings, encoded_payload)
    if not hmac.compare_digest(signature, expected_signature):
        return False

    try:
        padding = "=" * (-len(encoded_payload) % 4)
        issued_at = float(base64.urlsafe_b64decode(encoded_payload + padding).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return False

    now = time.time() if now is None else now
    return 0 <= now - issued_at <= ttl_seconds


def _sign(settings: Settings, encoded_payload: str) -> str:
    return hmac.new(
        settings.web_session_secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

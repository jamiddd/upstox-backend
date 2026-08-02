from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from app.core.exceptions import AppConfigError

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Environment-backed settings for the API service."""

    upstox_api_key: str
    upstox_api_secret: str
    upstox_redirect_url: str
    upstox_environment: str
    mobile_api_key: str
    token_encryption_key: str
    token_store_path: Path
    # Credentials for the fully-automated TOTP login (UpstoxTotpLoginService/auto_login_scheduler)
    # -- Upstox mobile number, the TOTP secret (base32, from the same authenticator-app setup a
    # human would use), and the account's transaction PIN. Deliberately NOT the account's actual
    # login password -- the reverse-engineered login sequence never uses it (mobile number + TOTP
    # + PIN is sufficient). Plain env vars, same as every other secret in this file (upstox_api_secret,
    # mobile_api_key) -- only the resulting access token gets Fernet-encrypted at rest.
    upstox_totp_username: str = ""
    upstox_totp_secret: str = ""
    upstox_totp_pin: str = ""
    tracked_instruments_path: Path = Path("/data/tracked_instruments.json")
    watchlist_path: Path = Path("/data/watchlist.json")
    account_snapshot_path: Path = Path("/data/account_snapshot.json")
    auto_login_state_path: Path = Path("/data/auto_login_state.json")
    oi_database_path: Path = Path("/data/oi_snapshots.sqlite3")
    notification_database_path: Path = Path("/data/notifications.sqlite3")
    journal_database_path: Path = Path("/data/journal.sqlite3")
    device_token_path: Path = Path("/data/device_token.json")
    max_loss_settings_path: Path = Path("/data/max_loss_settings.json")
    log_file_path: Path = Path("/data/logs/app.log")
    notification_retention_days: int = 90
    firebase_service_account_path: Path = Path("/data/firebase_service_account.json")
    upstox_api_base_url: str = "https://api.upstox.com/v2"
    upstox_api_v3_base_url: str = "https://api.upstox.com/v3"
    # Place Order V3 is only documented on this separate low-latency host, not api.upstox.com --
    # see UpstoxService.place_market_order.
    upstox_api_hft_base_url: str = "https://api-hft.upstox.com/v3"
    upstox_login_url: str = "https://api.upstox.com/v2/login/authorization/dialog"
    upstox_token_url: str = "https://api.upstox.com/v2/login/authorization/token"
    upstox_instrument_master_url: str = (
        "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
    )
    # Where /auth/callback sends the in-app browser after a successful Upstox login, so it closes
    # and hands control straight back to the app instead of leaving the user staring at a raw
    # JSON response -- see MainActivity's matching intent-filter (scheme "personalscalper", host
    # "auth", path "/callback") in the Android app repo. A custom URI scheme, not a real https
    # URL -- Android resolves it straight to the app via Chrome Custom Tabs' normal redirect
    # handling, no App Links/asset-links.json verification needed.
    mobile_app_redirect_url: str = "personalscalper://auth/callback"
    # CORS origin for the SvelteKit web client (e.g. "https://app.scalp8.xyz"). Empty disables
    # CORS entirely -- the Android app never needs it (no browser same-origin policy applies to
    # its OkHttp calls), so an unset value here does not block Android in any way.
    web_client_origin: str = ""
    # HMAC signing key for the web client's session cookie (see app/core/web_session.py) --
    # entirely separate from mobile_api_key: this secret only ever signs/verifies a cookie minted
    # by this backend, it is never sent by or exposed to the browser itself.
    web_session_secret: str = ""
    # Where /auth/callback sends the browser after a successful Upstox login when the login was
    # initiated by the web client (state=web) -- the browser equivalent of mobile_app_redirect_url,
    # e.g. "https://app.scalp8.xyz/auth/callback".
    web_client_auth_redirect_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        """Create settings from environment variables and .env values."""
        return cls(
            upstox_api_key=os.getenv("UPSTOX_API_KEY", ""),
            upstox_api_secret=os.getenv("UPSTOX_API_SECRET", ""),
            upstox_redirect_url=os.getenv("UPSTOX_REDIRECT_URL", ""),
            upstox_environment=os.getenv("UPSTOX_ENVIRONMENT", "sandbox"),
            mobile_api_key=os.getenv("MOBILE_API_KEY", ""),
            upstox_totp_username=os.getenv("UPSTOX_TOTP_USERNAME", ""),
            upstox_totp_secret=os.getenv("UPSTOX_TOTP_SECRET", ""),
            upstox_totp_pin=os.getenv("UPSTOX_TOTP_PIN", ""),
            token_encryption_key=os.getenv("TOKEN_ENCRYPTION_KEY", ""),
            token_store_path=Path(os.getenv("TOKEN_STORE_PATH", "/data/upstox_token.enc")),
            tracked_instruments_path=Path(
                os.getenv("TRACKED_INSTRUMENTS_PATH", "/data/tracked_instruments.json"),
            ),
            watchlist_path=Path(
                os.getenv("WATCHLIST_PATH", "/data/watchlist.json"),
            ),
            account_snapshot_path=Path(
                os.getenv("ACCOUNT_SNAPSHOT_PATH", "/data/account_snapshot.json"),
            ),
            auto_login_state_path=Path(
                os.getenv("AUTO_LOGIN_STATE_PATH", "/data/auto_login_state.json"),
            ),
            oi_database_path=Path(
                os.getenv("OI_DATABASE_PATH", "/data/oi_snapshots.sqlite3"),
            ),
            notification_database_path=Path(
                os.getenv("NOTIFICATION_DATABASE_PATH", "/data/notifications.sqlite3"),
            ),
            journal_database_path=Path(
                os.getenv("JOURNAL_DATABASE_PATH", "/data/journal.sqlite3"),
            ),
            device_token_path=Path(
                os.getenv("DEVICE_TOKEN_PATH", "/data/device_token.json"),
            ),
            max_loss_settings_path=Path(
                os.getenv("MAX_LOSS_SETTINGS_PATH", "/data/max_loss_settings.json"),
            ),
            log_file_path=Path(os.getenv("LOG_FILE_PATH", "/data/logs/app.log")),
            notification_retention_days=max(
                1, int(os.getenv("NOTIFICATION_RETENTION_DAYS", "90")),
            ),
            firebase_service_account_path=Path(
                os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "/data/firebase_service_account.json"),
            ),
            upstox_api_base_url=os.getenv("UPSTOX_API_BASE_URL", "https://api.upstox.com/v2"),
            upstox_api_v3_base_url=os.getenv(
                "UPSTOX_API_V3_BASE_URL",
                "https://api.upstox.com/v3",
            ),
            upstox_api_hft_base_url=os.getenv(
                "UPSTOX_API_HFT_BASE_URL",
                "https://api-hft.upstox.com/v3",
            ),
            upstox_login_url=os.getenv(
                "UPSTOX_LOGIN_URL",
                "https://api.upstox.com/v2/login/authorization/dialog",
            ),
            upstox_token_url=os.getenv(
                "UPSTOX_TOKEN_URL",
                "https://api.upstox.com/v2/login/authorization/token",
            ),
            upstox_instrument_master_url=os.getenv(
                "UPSTOX_INSTRUMENT_MASTER_URL",
                "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz",
            ),
            mobile_app_redirect_url=os.getenv(
                "MOBILE_APP_REDIRECT_URL",
                "personalscalper://auth/callback",
            ),
            web_client_origin=os.getenv("WEB_CLIENT_ORIGIN", ""),
            web_session_secret=os.getenv("WEB_SESSION_SECRET", ""),
            web_client_auth_redirect_url=os.getenv("WEB_CLIENT_AUTH_REDIRECT_URL", ""),
        )

    def require_mobile_api_key(self) -> None:
        """Ensure the backend API key has been configured."""
        if not self.mobile_api_key:
            raise AppConfigError("MOBILE_API_KEY is not configured")

    def require_upstox_oauth(self) -> None:
        """Ensure OAuth credentials are configured before starting login."""
        missing = [
            name
            for name, value in (
                ("UPSTOX_API_KEY", self.upstox_api_key),
                ("UPSTOX_API_SECRET", self.upstox_api_secret),
                ("UPSTOX_REDIRECT_URL", self.upstox_redirect_url),
            )
            if not value
        ]
        if missing:
            raise AppConfigError(f"Missing Upstox OAuth settings: {', '.join(missing)}")
        parsed = urlparse(self.upstox_redirect_url)
        if not parsed.scheme or not parsed.netloc:
            raise AppConfigError("UPSTOX_REDIRECT_URL must be an absolute URL")

    def require_web_session_secret(self) -> None:
        """Ensure the web client's session-signing secret has been configured."""
        if not self.web_session_secret:
            raise AppConfigError("WEB_SESSION_SECRET is not configured")

    def require_upstox_totp_login(self) -> None:
        """Ensure the automated TOTP login's own credentials are configured, on top of the
        regular OAuth app credentials (UpstoxTotpLoginService also needs client_id/client_secret/
        redirect_url, checked separately by require_upstox_oauth)."""
        self.require_upstox_oauth()
        missing = [
            name
            for name, value in (
                ("UPSTOX_TOTP_USERNAME", self.upstox_totp_username),
                ("UPSTOX_TOTP_SECRET", self.upstox_totp_secret),
                ("UPSTOX_TOTP_PIN", self.upstox_totp_pin),
            )
            if not value
        ]
        if missing:
            raise AppConfigError(f"Missing Upstox TOTP login settings: {', '.join(missing)}")


def get_settings() -> Settings:
    """Return settings loaded from the current process environment."""
    return Settings.from_env()


settings = get_settings()

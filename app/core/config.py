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
    upstox_api_base_url: str = "https://api.upstox.com/v2"
    upstox_api_v3_base_url: str = "https://api.upstox.com/v3"
    upstox_login_url: str = "https://api.upstox.com/v2/login/authorization/dialog"
    upstox_token_url: str = "https://api.upstox.com/v2/login/authorization/token"

    @classmethod
    def from_env(cls) -> "Settings":
        """Create settings from environment variables and .env values."""
        return cls(
            upstox_api_key=os.getenv("UPSTOX_API_KEY", ""),
            upstox_api_secret=os.getenv("UPSTOX_API_SECRET", ""),
            upstox_redirect_url=os.getenv("UPSTOX_REDIRECT_URL", ""),
            upstox_environment=os.getenv("UPSTOX_ENVIRONMENT", "sandbox"),
            mobile_api_key=os.getenv("MOBILE_API_KEY", ""),
            token_encryption_key=os.getenv("TOKEN_ENCRYPTION_KEY", ""),
            token_store_path=Path(os.getenv("TOKEN_STORE_PATH", "/data/upstox_token.enc")),
            upstox_api_base_url=os.getenv("UPSTOX_API_BASE_URL", "https://api.upstox.com/v2"),
            upstox_api_v3_base_url=os.getenv(
                "UPSTOX_API_V3_BASE_URL",
                "https://api.upstox.com/v3",
            ),
            upstox_login_url=os.getenv(
                "UPSTOX_LOGIN_URL",
                "https://api.upstox.com/v2/login/authorization/dialog",
            ),
            upstox_token_url=os.getenv(
                "UPSTOX_TOKEN_URL",
                "https://api.upstox.com/v2/login/authorization/token",
            ),
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


def get_settings() -> Settings:
    """Return settings loaded from the current process environment."""
    return Settings.from_env()


settings = get_settings()

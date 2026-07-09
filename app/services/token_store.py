from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxAuthRequiredError


class EncryptedTokenStore:
    """Persist the Upstox token response in an encrypted local file."""

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.token_store_path)
        self._fernet = self._build_fernet(settings.token_encryption_key)

    def has_token(self) -> bool:
        """Return whether an encrypted token file exists."""
        return self.path.exists()

    def save(self, token_payload: dict[str, Any]) -> None:
        """Encrypt and save the complete Upstox token payload."""
        if "access_token" not in token_payload:
            raise TokenStoreError("Upstox token response did not include access_token")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(token_payload).encode("utf-8")
        self.path.write_bytes(self._fernet.encrypt(encoded))

    def load(self) -> dict[str, Any]:
        """Decrypt and return the stored Upstox token payload."""
        if not self.path.exists():
            raise UpstoxAuthRequiredError("Upstox login is required")

        try:
            decrypted = self._fernet.decrypt(self.path.read_bytes())
            payload = json.loads(decrypted.decode("utf-8"))
        except (InvalidToken, OSError, json.JSONDecodeError) as exc:
            raise TokenStoreError("Unable to read encrypted Upstox token store") from exc

        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise TokenStoreError("Encrypted Upstox token store is invalid")
        return payload

    def load_access_token(self) -> str:
        """Return the stored access token."""
        token = self.load()["access_token"]
        if not isinstance(token, str):
            raise TokenStoreError("Encrypted Upstox access token is invalid")
        return token

    def clear(self) -> None:
        """Delete the encrypted token file when present."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise TokenStoreError("Unable to delete encrypted Upstox token store") from exc

    @staticmethod
    def _build_fernet(encryption_key: str) -> Fernet:
        """Create a Fernet instance from the configured encryption key."""
        if not encryption_key:
            raise TokenStoreError("TOKEN_ENCRYPTION_KEY is not configured")
        try:
            return Fernet(encryption_key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise TokenStoreError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc

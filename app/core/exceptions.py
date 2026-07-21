from __future__ import annotations

from typing import Optional


class AppConfigError(RuntimeError):
    """Raised when required application configuration is missing or invalid."""


class TokenStoreError(RuntimeError):
    """Raised when the encrypted Upstox token store cannot be used."""


class TrackedInstrumentsStoreError(RuntimeError):
    """Raised when the tracked-instruments store cannot be used."""


class PendingOcoPairsStoreError(RuntimeError):
    """Raised when the pending-OCO-pairs store cannot be used."""


class UpstoxAuthRequiredError(RuntimeError):
    """Raised when an Upstox-backed route is called before login."""


class UpstoxApiError(RuntimeError):
    """Wrap an error response returned by Upstox."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        upstox_code: Optional[str] = None,
        details: Optional[object] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.upstox_code = upstox_code
        self.details = details

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from app.core.config import Settings

# Matches NotificationStore.SEVERITIES ordering -- kept as a separate literal here (rather than
# importing NotificationStore just for this) to avoid a needless cross-module dependency between
# two otherwise-independent small stores.
_PUSH_PREFERENCES = ("off", "critical", "everything")
_DEFAULT_PUSH_PREFERENCE = "critical"


class DeviceTokenStore:
    """Persists the single device's FCM push token and push-severity preference.

    A small flat JSON file, same posture as `TrackedInstrumentsStore` -- this is a personal,
    single-user, effectively single-device app, so a SQLite table would be overkill for one row of
    data. Not sensitive (an FCM token is only useful in combination with this backend's own
    Firebase service-account credentials), so plain JSON, not Fernet-encrypted like the Upstox
    token store.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.device_token_path)

    def load(self) -> tuple[Optional[str], str]:
        """Returns `(fcm_token, push_preference)` -- `fcm_token` is `None` if no device has ever
        registered; `push_preference` defaults to `"critical"` (matches the app's own default)
        rather than `"off"`, so a stale/corrupt file doesn't silently swallow critical alerts."""
        if not self.path.exists():
            return None, _DEFAULT_PUSH_PREFERENCE
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, _DEFAULT_PUSH_PREFERENCE
        if not isinstance(payload, dict):
            return None, _DEFAULT_PUSH_PREFERENCE
        token = payload.get("fcm_token")
        preference = payload.get("push_preference")
        return (
            token if isinstance(token, str) and token else None,
            preference if preference in _PUSH_PREFERENCES else _DEFAULT_PUSH_PREFERENCE,
        )

    def save(self, *, fcm_token: Optional[str], push_preference: str) -> None:
        """Replaces the whole persisted state -- the client always sends its current token and
        preference together (see `POST /api/notifications/register-device`), not an incremental
        update, so this always overwrites."""
        if push_preference not in _PUSH_PREFERENCES:
            raise ValueError(f"Unknown push preference {push_preference!r}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"fcm_token": fcm_token, "push_preference": push_preference}),
            encoding="utf-8",
        )

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings


class MaxLossSettingsStore:
    """Persists the single configured max-loss threshold (rupees, absolute value) that both the
    app's own client-side watchdog (MainViewModel.checkMaxLoss) and this backend's own
    max_loss_watcher.py enforce against. `0.0` (the default) means disabled -- matches
    AppSettingsRepository.maxLossAmount's own "0 or less turns it off" convention on the client.

    A small flat JSON file, same posture as `DeviceTokenStore`/`TrackedInstrumentsStore` -- this
    is a personal, single-user app, so a SQLite table would be overkill for one number.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.max_loss_settings_path)

    def load(self) -> float:
        if not self.path.exists():
            return 0.0
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0.0
        if not isinstance(payload, dict):
            return 0.0
        amount = payload.get("amount")
        return float(amount) if isinstance(amount, (int, float)) and not isinstance(amount, bool) else 0.0

    def save(self, amount: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"amount": amount}), encoding="utf-8")

    def clear(self) -> None:
        """Disarms the threshold (sets it back to 0) -- called once the watcher itself has fired,
        same "consumed, needs a fresh explicit re-arm" semantics as the client zeroing its own
        locally-persisted amount after triggering."""
        self.save(0.0)

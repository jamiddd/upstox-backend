from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.core.exceptions import TrackedInstrumentsStoreError


class TrackedInstrumentsStore:
    """Persists the user's Settings-picked list of underlying_keys to poll for 5-minute-change
    history in the background (see `app.main`'s startup poller and
    `UnderlyingSignalsService._record_and_diff`) -- so the list survives a server/container
    restart without the Android client having to notice and re-push it.

    A small flat JSON file, same posture as [EncryptedTokenStore] -- this repo is deliberately
    database-free (see README's "Keep the first version database-free" goal), and a handful of
    instrument keys don't warrant one. Unlike the token store, this isn't sensitive, so it's
    plain JSON, not Fernet-encrypted.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.tracked_instruments_path)

    def load(self) -> list[str]:
        """Return the persisted underlying_keys, or an empty list if nothing's been saved yet
        (or the file is unreadable/corrupt -- degrades to "nothing tracked", not a crash, same
        "missing data means omit" posture as the rest of this backend)."""
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        keys = payload.get("underlying_keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list):
            return []
        return [key for key in keys if isinstance(key, str) and key]

    def save(self, underlying_keys: list[str]) -> None:
        """Replaces the whole persisted set -- the client always sends its full current
        selection (see the PUT /api/user/tracked-instruments route), not an incremental
        add/remove, so this always overwrites rather than merging."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # De-duplicated, order-preserved -- the client's own settings list shouldn't have
        # duplicates, but this stays correct even if it ever does.
        deduped = list(dict.fromkeys(key for key in underlying_keys if key))
        try:
            self.path.write_text(json.dumps({"underlying_keys": deduped}), encoding="utf-8")
        except OSError as exc:
            raise TrackedInstrumentsStoreError("Unable to write tracked-instruments store") from exc

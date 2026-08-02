from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.core.exceptions import WatchlistStoreError

WATCHLIST_IDS = ("india", "global")


class WatchlistStore:
    """Persists the two ticker watchlists (India, Global) shared by both the Android app and the
    web client -- see `docs/WATCHLIST_API.md`. Same posture as `TrackedInstrumentsStore`: a
    handful of instruments per list doesn't warrant a database table, so this is a small flat
    JSON file, not SQLite (same reasoning `MaxLossSettingsStore` documents for itself).

    Each list is always replaced wholesale on save, never merged -- the client always sends its
    full current selection (add/remove/reorder are all local UI operations that end in one PUT of
    the resulting list), same contract as TrackedInstrumentsStore.save.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.watchlist_path)

    def _read_all(self) -> dict[str, list[dict]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def load(self, list_id: str) -> list[dict]:
        """Return the persisted items for `list_id`, or an empty list if nothing's been saved
        yet (or the file is unreadable/corrupt -- degrades to "nothing saved", not a crash, same
        posture as the rest of this backend's flat-file stores)."""
        items = self._read_all().get(list_id)
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict) and item.get("instrument_key")]

    def save(self, list_id: str, items: list[dict]) -> None:
        """Replaces the whole persisted list for `list_id`. De-duplicated by instrument_key,
        order-preserved -- the client's own list shouldn't have duplicates, but this stays
        correct even if it ever does."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deduped: dict[str, dict] = {}
        for item in items:
            key = item.get("instrument_key")
            if key:
                deduped[key] = item
        all_lists = self._read_all()
        all_lists[list_id] = list(deduped.values())
        try:
            self.path.write_text(json.dumps(all_lists), encoding="utf-8")
        except OSError as exc:
            raise WatchlistStoreError("Unable to write watchlist store") from exc

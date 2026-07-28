from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from app.core.config import Settings


@dataclass(frozen=True)
class AutoLoginAttemptState:
    """Record automated-login attempts for one calendar day."""

    date: str
    attempt_count: int
    succeeded: bool


class AutoLoginStateStore:
    """Persist daily auto-login state in a small JSON file.

    Keeping this state across restarts prevents successful logins from being repeated and failed
    attempts from exceeding the daily limit enforced by the scheduler.
    """

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.auto_login_state_path)

    def load(self) -> Optional[AutoLoginAttemptState]:
        """Return the saved state, or ``None`` when it is absent, invalid, or unreadable."""
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None

        return _parse_state(payload)

    def save(self, state: AutoLoginAttemptState) -> None:
        """Atomically replace the stored state."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(asdict(state), separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        temporary_path.replace(self.path)


def _parse_state(payload: Any) -> Optional[AutoLoginAttemptState]:
    if not isinstance(payload, dict):
        return None

    state_date = payload.get("date")
    attempt_count = payload.get("attempt_count")
    succeeded = payload.get("succeeded")

    if not isinstance(state_date, str) or not state_date:
        return None
    if type(attempt_count) is not int or attempt_count < 0:
        return None
    if type(succeeded) is not bool:
        return None

    return AutoLoginAttemptState(
        date=state_date,
        attempt_count=attempt_count,
        succeeded=succeeded,
    )

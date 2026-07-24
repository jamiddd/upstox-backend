from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings


@dataclass(frozen=True)
class AccountSnapshot:
    estimated_balance: float
    captured_at: str

    @property
    def captured_date(self) -> str:
        return self.captured_at[:10]


class AccountSnapshotStore:
    """Small persistent previous-close estimate used during the funds maintenance window."""

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.account_snapshot_path)

    def load(self) -> AccountSnapshot | None:
        if not self.path.exists():
            return None
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
            balance = payload.get("estimated_balance") if isinstance(payload, dict) else None
            captured_at = payload.get("captured_at") if isinstance(payload, dict) else None
            if not isinstance(balance, (int, float)) or balance <= 0:
                return None
            if not isinstance(captured_at, str):
                return None
            datetime.fromisoformat(captured_at)
            return AccountSnapshot(float(balance), captured_at)
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def save(self, snapshot: AccountSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(snapshot)), encoding="utf-8")
        temporary.replace(self.path)

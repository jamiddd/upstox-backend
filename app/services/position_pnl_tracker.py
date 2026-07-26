from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService


@dataclass
class _PositionSnapshot:
    pnl: float
    last_price: float
    quantity: float


class PositionPnlTracker:
    """Live, tick-driven total P&L across every open position -- lets max_loss_watcher react to
    the market feed's own live ticks instead of polling `get_positions()` on a timer.

    Two update paths:
    - `refresh()` -- a real REST call to `get_positions()`, expensive-ish (network round trip),
      called relatively rarely (a slow periodic fallback, and immediately on every portfolio-feed
      order-update event -- see `app/main.py`'s wiring). Replaces the whole cached snapshot.
    - `apply_tick(instrument_key, ltp)` -- cheap, called on every live market tick for an
      instrument that's part of an open position. Same delta-from-last-known-snapshot formula
      Android's own MainViewModel.handleTick already uses for its live P&L display:
      `live_pnl = snapshot_pnl + (tick_ltp - snapshot_last_price) * quantity` -- avoids needing to
      know lot-size/multiplier conventions ourselves, since it only tracks the *change* since the
      last REST-confirmed `pnl`/`last_price` pair (which Upstox already computed correctly).

    `total_pnl()` is what `max_loss_watcher.check_now` actually compares against the threshold --
    it's the sum of each position's live-adjusted P&L, not a single REST field, so it reflects
    ticks that have arrived since the last `refresh()` for every position at once, not just
    whichever one happens to have ticked most recently.
    """

    def __init__(self, upstox: UpstoxService, token_store: EncryptedTokenStore) -> None:
        self._upstox = upstox
        self._token_store = token_store
        self._positions: dict[str, _PositionSnapshot] = {}
        self._live_ltp: dict[str, float] = {}

    async def refresh(self) -> None:
        """Re-fetches every position (open and today's already-closed) from Upstox and replaces
        the cached snapshot wholesale. Silently leaves the previous snapshot in place on any
        failure (no token yet, Upstox unreachable) -- same degrade-gracefully posture as the rest
        of this backend's background pollers; a stale-by-one-refresh snapshot is far better than
        losing max-loss coverage entirely because of one transient failure.
        """
        if not self._token_store.has_token():
            return
        try:
            access_token = self._token_store.load_access_token()
        except (TokenStoreError, UpstoxAuthRequiredError):
            return
        try:
            payload = await self._upstox.get_positions(access_token)
        except UpstoxApiError:
            return

        data = payload.get("data")
        positions = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        snapshots: dict[str, _PositionSnapshot] = {}
        for position in positions:
            key = _string_value(position, "instrument_token", "instrument_key")
            if not key:
                continue
            snapshots[key] = _PositionSnapshot(
                pnl=_number(position.get("pnl")),
                last_price=_number(position.get("last_price")),
                quantity=_number(position.get("quantity")),
            )
        self._positions = snapshots
        # Ticks for an instrument no longer in the fresh snapshot (position fully closed since
        # the last refresh) are meaningless going forward -- drop them so a later re-entry into
        # the same instrument doesn't start from a stale, unrelated live price.
        self._live_ltp = {key: ltp for key, ltp in self._live_ltp.items() if key in snapshots}

    def apply_tick(self, instrument_key: str, ltp: Optional[float]) -> None:
        if ltp is None or instrument_key not in self._positions:
            return
        self._live_ltp[instrument_key] = ltp

    def total_pnl(self) -> float:
        total = 0.0
        for key, snapshot in self._positions.items():
            live_ltp = self._live_ltp.get(key, snapshot.last_price)
            total += snapshot.pnl + (live_ltp - snapshot.last_price) * snapshot.quantity
        return total

    def instrument_keys(self) -> set[str]:
        """Every currently open position's instrument -- what `FeedSubscriptionManager` needs to
        keep subscribed on the shared market feed so `apply_tick` actually gets called for them,
        independent of whether any app session happens to have that instrument open too."""
        return set(self._positions.keys())


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _string_value(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str):
            return value
    return ""

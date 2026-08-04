from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.services.journal_store import JournalStore
from app.services.notification_service import NotificationService
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class JournalReconciler:
    """Token-aware, idempotent current-day fill reconciliation."""

    def __init__(
        self,
        *,
        store: JournalStore,
        upstox: UpstoxService,
        token_store: EncryptedTokenStore,
        notifications: Optional[NotificationService] = None,
    ) -> None:
        self.store = store
        self.upstox = upstox
        self.token_store = token_store
        self.notifications = notifications
        self._lock = asyncio.Lock()

    async def reconcile(self, *, complete: bool = False) -> dict[str, Any]:
        if not self.token_store.has_token():
            return {"status": "waiting_for_auth", "fills": 0}
        async with self._lock:
            try:
                access_token = self.token_store.load_access_token()
                payload = await self.upstox.get_trades_for_day(access_token)
                raw_trades = payload.get("data")
                trades = raw_trades if isinstance(raw_trades, list) else []
                inserted = 0
                trading_date = datetime.now(IST).date().isoformat()
                # Charges already on file, keyed by fill_id -- used so a failed
                # `/charges/brokerage` call (rate limit, timeout, ...) falls back to the last
                # known-good value instead of clobbering it with 0.0. Without this, a burst of
                # fills landing in the same second (e.g. a stop-loss cascade) tends to hit
                # Upstox's rate limit together and each reconcile pass re-zeroes them forever,
                # since upsert_fill unconditionally overwrites computed_charges every pass.
                fill_ids = [_text(raw, "trade_id", "fill_id") for raw in trades if isinstance(raw, dict)]
                known_charges = self.store.charges_for_fill_ids([f for f in fill_ids if f])
                for raw in trades:
                    if not isinstance(raw, dict):
                        continue
                    fill = _normalize_fill(raw, fallback_date=trading_date)
                    if fill is None:
                        logger.warning("Skipping malformed broker trade: %r", raw)
                        continue
                    charges = await self._charges(access_token, fill)
                    fill["computed_charges"] = (
                        charges if charges is not None else known_charges.get(fill["fill_id"], 0.0)
                    )
                    inserted += int(self.store.upsert_fill(fill))
                    trading_date = fill["trade_date"]
                    # Stagger sequential brokerage-charge calls so a batch of same-second fills
                    # (stop-loss cascades) doesn't hammer Upstox's rate limit as one burst.
                    await asyncio.sleep(0.2)
                self.store.record_session_sync(trading_date, complete=complete)
                conflicts = self.store.rebuild_session(trading_date)
                if conflicts and self.notifications:
                    await self.notifications.record(
                        category="system",
                        severity="warning",
                        title="Journal trade needs review",
                        message="A late fill changed a trade that already has journal notes.",
                        details={"trade_ids": conflicts, "trading_date": trading_date},
                    )
                return {
                    "status": "current",
                    "fills": len(trades),
                    "inserted": inserted,
                    "conflicts": len(conflicts),
                }
            except Exception:
                logger.warning("Journal reconciliation failed", exc_info=True)
                return {"status": "error", "fills": 0}

    async def _charges(self, access_token: str, fill: dict[str, Any]) -> Optional[float]:
        """Returns None (rather than 0.0) on any failure, so the caller can fall back to the
        last known-good charge instead of overwriting it with a spurious zero."""
        try:
            payload = await self.upstox.get_brokerage(
                access_token,
                instrument_key=fill["instrument_key"],
                quantity=max(1, int(fill["quantity"])),
                product="I",
                transaction_type=fill["transaction_type"],
                price=fill["price"],
            )
            data = payload.get("data")
            charges = data.get("charges") if isinstance(data, dict) else None
            total = charges.get("total") if isinstance(charges, dict) else None
            return float(total) if isinstance(total, (int, float)) else None
        except Exception:
            logger.warning("Charge computation failed for fill %s", fill["fill_id"], exc_info=True)
            return None


async def run_journal_reconciler(reconciler: JournalReconciler) -> None:
    """Periodic market-hours reconciliation; OAuth and feed events also trigger it immediately."""
    while True:
        now = datetime.now(IST)
        if now.weekday() < 5 and time(9, 0) <= now.time() <= time(16, 0):
            await reconciler.reconcile(complete=now.time() >= time(15, 45))
        await asyncio.sleep(60)


def _normalize_fill(raw: dict[str, Any], *, fallback_date: str) -> Optional[dict[str, Any]]:
    fill_id = _text(raw, "trade_id", "fill_id")
    order_id = _text(raw, "order_id")
    instrument_key = _text(raw, "instrument_token", "instrument_key")
    side = _text(raw, "transaction_type").upper()
    quantity = _number(raw, "quantity", "filled_quantity")
    price = _number(raw, "average_price", "price")
    if not fill_id or not order_id or not instrument_key or side not in {"BUY", "SELL"}:
        return None
    timestamp = _text(raw, "exchange_timestamp", "order_timestamp", "trade_timestamp")
    trade_date = _text(raw, "trade_date") or (timestamp[:10] if len(timestamp) >= 10 else fallback_date)
    executed_at = _utc_timestamp(timestamp, trade_date)
    return {
        "fill_id": fill_id,
        "order_id": order_id,
        "instrument_key": instrument_key,
        "trading_symbol": _text(raw, "trading_symbol", "tradingsymbol", "symbol"),
        "transaction_type": side,
        "quantity": quantity,
        "price": price,
        "executed_at": executed_at,
        "trade_date": trade_date,
        "exchange": raw.get("exchange"),
        "segment": raw.get("segment"),
        "option_type": raw.get("option_type"),
        "strike_price": raw.get("strike_price"),
        "expiry": raw.get("expiry"),
        "raw_payload": raw,
    }


def _utc_timestamp(value: str, trade_date: str) -> str:
    for candidate in (value, value.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=IST)
            return parsed.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.combine(date.fromisoformat(trade_date), time.min, IST).astimezone(
        ZoneInfo("UTC"),
    ).isoformat(timespec="seconds")


def _text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _number(payload: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0

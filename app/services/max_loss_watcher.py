from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.core.market_hours import is_market_open
from app.services.instrument_rules_service import InstrumentRulesService
from app.services.max_loss_settings_store import MaxLossSettingsStore
from app.services.notification_service import NotificationService
from app.services.smart_order_service import SmartOrderService
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_LOOP_INTERVAL_SECONDS = 5.0


class _TokenStoreProtocol(Protocol):
    def has_token(self) -> bool: ...
    def load_access_token(self) -> str: ...


class _MaxLossSettingsStoreProtocol(Protocol):
    def load(self) -> float: ...
    def clear(self) -> None: ...


class _UpstoxServiceProtocol(Protocol):
    async def get_positions(self, access_token: str) -> dict[str, Any]: ...


class _SmartOrderServiceProtocol(Protocol):
    async def exit_all_positions(
        self, access_token: str, *, instrument_rules_service: InstrumentRulesService,
    ) -> dict[str, Any]: ...


async def run_max_loss_watcher(
    settings: Settings,
    notification_service: NotificationService,
    exit_all_lock: asyncio.Lock,
) -> None:
    """Backend-side backstop for max-loss auto square-off -- reacts even if the app is closed,
    backgrounded, or offline, unlike MainViewModel.checkMaxLoss (foreground, tick-driven only).
    Both stay running at once: whichever notices a breach first flattens everything; the other
    finds nothing left open on its own next check. [exit_all_lock] is the same lock
    `POST /orders/exit-all` holds while flattening, so a client-triggered flatten and this
    watcher's own can never race into a double-exit against the same still-open position (see
    that route's own doc comment).
    """
    token_store = EncryptedTokenStore(settings)
    settings_store = MaxLossSettingsStore(settings)
    upstox = UpstoxService(settings)
    smart_order_service = SmartOrderService(upstox)
    instrument_rules_service = InstrumentRulesService(settings)

    while True:
        try:
            await _check_once(
                now=datetime.now(_IST),
                token_store=token_store,
                settings_store=settings_store,
                upstox=upstox,
                smart_order_service=smart_order_service,
                instrument_rules_service=instrument_rules_service,
                notification_service=notification_service,
                exit_all_lock=exit_all_lock,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Max-loss watcher tick failed unexpectedly")
        await asyncio.sleep(_LOOP_INTERVAL_SECONDS)


async def _check_once(
    *,
    now: datetime,
    token_store: _TokenStoreProtocol,
    settings_store: _MaxLossSettingsStoreProtocol,
    upstox: _UpstoxServiceProtocol,
    smart_order_service: _SmartOrderServiceProtocol,
    instrument_rules_service: InstrumentRulesService,
    notification_service: NotificationService,
    exit_all_lock: asyncio.Lock,
) -> None:
    if not is_market_open(now.astimezone(_IST)):
        return

    threshold = settings_store.load()
    if threshold <= 0.0:
        return

    if not token_store.has_token():
        return
    try:
        access_token = token_store.load_access_token()
    except (TokenStoreError, UpstoxAuthRequiredError):
        return

    try:
        positions_payload = await upstox.get_positions(access_token)
    except UpstoxApiError:
        return

    pnl = _positions_pnl(positions_payload)
    if pnl > -threshold:
        return

    async with exit_all_lock:
        # Re-read: the client's own check may have already fired (and disarmed the threshold)
        # in the moment between the check above and actually acquiring the lock.
        if settings_store.load() <= 0.0:
            return

        try:
            result = await smart_order_service.exit_all_positions(
                access_token, instrument_rules_service=instrument_rules_service,
            )
        except UpstoxApiError as exc:
            logger.warning("Max-loss watcher: flatten-all failed", exc_info=True)
            await notification_service.record(
                category="risk",
                severity="critical",
                title="Max-loss auto square-off failed",
                message=(
                    f"Backend detected a max-loss breach (P&L {pnl:.2f} against a "
                    f"-{threshold:.2f} threshold) but flattening failed: {exc}"
                ),
            )
            return

        settings_store.clear()
        logger.warning(
            "Max-loss watcher flattened all positions: pnl=%.2f threshold=%.2f", pnl, threshold,
        )
        await notification_service.record(
            category="risk",
            severity="critical",
            title="Max-loss auto square-off triggered",
            message=(
                f"Backend's own max-loss watcher flattened all positions "
                f"(P&L {pnl:.2f} breached the -{threshold:.2f} threshold)."
            ),
            details={"pnl": pnl, "threshold": threshold, "result": result},
        )


def _positions_pnl(positions_payload: dict[str, Any]) -> float:
    """Sums every position's own `pnl` field (including squared-off ones -- a closed position's
    realized P&L still counts toward today's total), same field Upstox reports and
    MainScreenService.summary's own profit_loss is derived from."""
    data = positions_payload.get("data")
    positions = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    total = 0.0
    for position in positions:
        pnl = position.get("pnl")
        if isinstance(pnl, (int, float)) and not isinstance(pnl, bool):
            total += float(pnl)
    return total

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.core.exceptions import TokenStoreError, UpstoxApiError, UpstoxAuthRequiredError
from app.core.market_hours import is_market_open
from app.services.instrument_rules_service import InstrumentRulesService
from app.services.notification_service import NotificationService
from app.services.position_pnl_tracker import PositionPnlTracker

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
# Purely a fallback safety net now (see check_now's own doc comment for the real, tick-driven
# reactivity path) -- catches a breach during a stretch with no live ticks at all (e.g. the
# market feed itself is reconnecting) using PositionPnlTracker's last-known cached total, no
# fresh REST call needed.
_FALLBACK_LOOP_INTERVAL_SECONDS = 5.0


class _TokenStoreProtocol(Protocol):
    def has_token(self) -> bool: ...
    def load_access_token(self) -> str: ...


class _MaxLossSettingsStoreProtocol(Protocol):
    def load(self) -> float: ...
    def clear(self) -> None: ...


class _SmartOrderServiceProtocol(Protocol):
    async def exit_all_positions(
        self, access_token: str, *, instrument_rules_service: InstrumentRulesService,
    ) -> dict[str, Any]: ...


async def run_max_loss_watcher_fallback(
    token_store: _TokenStoreProtocol,
    settings_store: _MaxLossSettingsStoreProtocol,
    tracker: PositionPnlTracker,
    smart_order_service: _SmartOrderServiceProtocol,
    instrument_rules_service: InstrumentRulesService,
    notification_service: NotificationService,
    exit_all_lock: asyncio.Lock,
) -> None:
    """Background fallback loop -- see `check_now`'s own doc comment for the primary, tick-driven
    reactivity path this backs up. Only matters when live ticks stop arriving for a stretch (e.g.
    the market feed itself is between reconnects); otherwise `check_now` fires on every tick well
    before this loop's own next iteration would.
    """
    while True:
        try:
            await check_now(
                now=datetime.now(_IST),
                token_store=token_store,
                settings_store=settings_store,
                tracker=tracker,
                smart_order_service=smart_order_service,
                instrument_rules_service=instrument_rules_service,
                notification_service=notification_service,
                exit_all_lock=exit_all_lock,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Max-loss fallback loop tick failed unexpectedly")
        await asyncio.sleep(_FALLBACK_LOOP_INTERVAL_SECONDS)


async def check_now(
    *,
    now: datetime,
    token_store: _TokenStoreProtocol,
    settings_store: _MaxLossSettingsStoreProtocol,
    tracker: PositionPnlTracker,
    smart_order_service: _SmartOrderServiceProtocol,
    instrument_rules_service: InstrumentRulesService,
    notification_service: NotificationService,
    exit_all_lock: asyncio.Lock,
) -> None:
    """The actual max-loss check -- called from `app.main`'s own market-tick handler on every
    live tick for an instrument that's part of an open position (see `PositionPnlTracker.apply_tick`
    and `FeedSubscriptionManager.set_open_position_instruments`), so a breach is caught within
    however fast Upstox's own feed ticks, not on a fixed polling interval. Also called by
    `run_max_loss_watcher_fallback` on a plain timer as a backstop for whenever ticks themselves
    stop flowing.

    Reads `tracker.total_pnl()` -- already-cached, tick-adjusted -- rather than making a fresh
    REST call itself; this function is cheap enough to call on every tick precisely because it
    does no I/O until (rarely) a breach actually needs flattening.
    """
    if not is_market_open(now.astimezone(_IST)):
        return

    threshold = settings_store.load()
    if threshold <= 0.0:
        return

    if tracker.total_pnl() > -threshold:
        return

    if not token_store.has_token():
        return
    try:
        access_token = token_store.load_access_token()
    except (TokenStoreError, UpstoxAuthRequiredError):
        return

    async with exit_all_lock:
        # Re-read: the client's own check may have already fired (and disarmed the threshold)
        # in the moment between the check above and actually acquiring the lock.
        if settings_store.load() <= 0.0:
            return
        # Re-read the live total too -- ticks (and this function being re-entered concurrently
        # for a different tick) don't wait for the lock, so the breach that triggered this call
        # may already be stale.
        pnl = tracker.total_pnl()
        if pnl > -threshold:
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

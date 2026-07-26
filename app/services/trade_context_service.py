from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from app.services.journal_store import JournalStore
from app.services.underlying_signals_service import UnderlyingSignalsService
from app.services.upstox_service import UpstoxService

logger = logging.getLogger(__name__)


class TradeContextService:
    """Best-effort capture of the irrecoverable market state around an order event."""

    def __init__(
        self,
        *,
        store: JournalStore,
        upstox: UpstoxService,
        signals: UnderlyingSignalsService,
    ) -> None:
        self.store = store
        self.upstox = upstox
        self.signals = signals

    async def capture(
        self,
        *,
        access_token: str,
        order_ids: Iterable[str],
        trigger: str,
        instrument_key: str,
        underlying_key: str,
        expiry_date: Optional[str],
        contract_ltp: Optional[float] = None,
    ) -> None:
        """Capture without propagating failures to the trading path.

        The row is still written with an empty JSON object when signals or quote retrieval fails.
        """
        ids = [value for value in order_ids if value]
        if not ids or not instrument_key or not underlying_key:
            logger.warning("Skipping trade-context capture with incomplete identifiers")
            return

        context: dict[str, Any] = {}
        try:
            context = await self.signals.get_signals(
                access_token,
                underlying_key=underlying_key,
                expiry_date=expiry_date,
            )
        except Exception:
            logger.warning("Trade-context signal capture failed", exc_info=True)

        resolved_ltp = contract_ltp
        if resolved_ltp is None:
            try:
                resolved_ltp = _extract_ltp(
                    await self.upstox.get_ltp(access_token, instrument_key),
                    instrument_key,
                )
            except Exception:
                logger.warning("Trade-context contract LTP capture failed", exc_info=True)

        for order_id in ids:
            try:
                self.store.record_context(
                    order_id=order_id,
                    trigger=trigger,
                    instrument_key=instrument_key,
                    underlying_key=underlying_key,
                    expiry_date=expiry_date,
                    contract_ltp=resolved_ltp,
                    context=context,
                )
            except Exception:
                logger.warning("Trade-context persistence failed for %s", order_id, exc_info=True)

    async def capture_fill_from_placement(
        self,
        *,
        access_token: str,
        order_id: str,
        instrument_key: Optional[str] = None,
        contract_ltp: Optional[float] = None,
    ) -> None:
        """Capture a fill using the underlying/expiry recorded at placement time.

        Feed payloads do not identify the dashboard underlying. A placement row is therefore the
        durable correlation record; fills not placed by this app are skipped until fill-ledger
        metadata can resolve them in the next phase.
        """
        placement = self.store.get_context(order_id, "placement")
        if placement is None and instrument_key:
            placement = self.store.latest_placement_context(instrument_key)
        if placement is None:
            logger.warning("No placement context available for fill order %s", order_id)
            return
        await self.capture(
            access_token=access_token,
            order_ids=[order_id],
            trigger="fill",
            instrument_key=placement["instrument_key"],
            underlying_key=placement["underlying_key"],
            expiry_date=placement["expiry_date"],
            contract_ltp=contract_ltp,
        )


def extract_order_ids(payload: Any) -> list[str]:
    """Return broker order/GTT IDs from arbitrarily nested Upstox responses."""
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"order_id", "gtt_order_id"} and isinstance(child, str) and child:
                    found.append(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return list(dict.fromkeys(found))


def _extract_ltp(payload: dict[str, Any], instrument_key: str) -> Optional[float]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    candidates = [data.get(instrument_key)]
    candidates.extend(data.values())
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("last_price", candidate.get("ltp"))
        if isinstance(value, (int, float)):
            return float(value)
    return None

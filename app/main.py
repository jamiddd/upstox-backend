from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.services.oco_watcher import run_oco_watcher
from app.services.account_snapshot_scheduler import run_account_snapshot_scheduler
from app.services.oi_snapshot_collector import run_oi_snapshot_collector
from app.services.tracked_instruments_poller import run_tracked_instruments_poller


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # See TrackedInstrumentsStore / run_tracked_instruments_poller's own doc comment for why this
    # exists -- keeps 5-minute-change history warm for Settings-picked underlyings even while no
    # client is actively polling. Cancelled cleanly on shutdown, same as any other background task
    # tied to the app's own lifetime.
    poller_task = asyncio.create_task(run_tracked_instruments_poller(settings))
    # See PendingOcoPairsStore / run_oco_watcher's own doc comment -- reconciles the plain
    # target/stoploss order pairs SmartOrderService.attach_gtt_exits places for a position with
    # no GTT bracket, independent of whether the app itself is open. Same lifecycle as the poller
    # above.
    oco_watcher_task = asyncio.create_task(run_oco_watcher(settings))
    oi_collector_task = asyncio.create_task(run_oi_snapshot_collector(settings))
    account_snapshot_task = asyncio.create_task(run_account_snapshot_scheduler(settings))
    try:
        yield
    finally:
        poller_task.cancel()
        oco_watcher_task.cancel()
        oi_collector_task.cancel()
        account_snapshot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller_task
        with contextlib.suppress(asyncio.CancelledError):
            await oco_watcher_task
        with contextlib.suppress(asyncio.CancelledError):
            await oi_collector_task
        with contextlib.suppress(asyncio.CancelledError):
            await account_snapshot_task


app = FastAPI(title="Upstox Scalper Backend", version="0.1.0", lifespan=_lifespan)
app.include_router(api_router, prefix="/api")


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return a simple health response for deployment checks."""
    return {"status": "ok"}


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """Return API error payloads without FastAPI's default detail wrapper."""
    content: Any = exc.detail
    if not isinstance(content, dict):
        content = {"status": "error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

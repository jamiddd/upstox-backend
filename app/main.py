from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.services.tracked_instruments_poller import run_tracked_instruments_poller


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # See TrackedInstrumentsStore / run_tracked_instruments_poller's own doc comment for why this
    # exists -- keeps 5-minute-change history warm for Settings-picked underlyings even while no
    # client is actively polling. Cancelled cleanly on shutdown, same as any other background task
    # tied to the app's own lifetime.
    poller_task = asyncio.create_task(run_tracked_instruments_poller(get_settings()))
    try:
        yield
    finally:
        poller_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller_task


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

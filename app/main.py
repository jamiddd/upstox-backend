from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.routes import router as api_router

app = FastAPI(title="Upstox Scalper Backend", version="0.1.0")
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

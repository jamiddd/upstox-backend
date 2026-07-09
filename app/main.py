from fastapi import FastAPI

from app.api.routes import router as api_router

app = FastAPI(title="Upstox Scalper Backend", version="0.1.0")
app.include_router(api_router, prefix="/api")


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return a simple health response for deployment checks."""
    return {"status": "ok"}

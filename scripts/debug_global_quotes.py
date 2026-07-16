"""One-off diagnostic: calls Upstox's Full Market Quote endpoint for each Global Instrument
key individually, to see which ones (if any) Upstox rejects. Run inside the backend container:

    docker compose exec api python3 scripts/debug_global_quotes.py

Not part of the app itself -- delete once the Global ticker issue is resolved.
"""
from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.core.exceptions import UpstoxApiError
from app.services.token_store import EncryptedTokenStore
from app.services.upstox_service import UpstoxService

KEYS = [
    "GLOBAL_INDEX|SGX NIFTY",
    "GLOBAL_INDEX|^GSPC",
    "GLOBAL_INDEX|IXIX",
    "GLOBAL_INDEX|^DJI",
    "GLOBAL_INDICATOR|USDINR",
    "GLOBAL_INDEX|^FTSE",
    "GLOBAL_INDEX|^GDAXI",
    "GLOBAL_INDEX|^FCHI",
    "GLOBAL_INDEX|^N225",
    "GLOBAL_INDEX|^HSI",
    "GLOBAL_INDICATOR|BZUSD",
    "GLOBAL_INDICATOR|CLUSD",
]


async def main() -> None:
    settings = Settings.from_env()
    token = EncryptedTokenStore(settings).load_access_token()
    svc = UpstoxService(settings)
    for key in KEYS:
        try:
            data = await svc.get_quotes(token, key)
            print("OK  ", key, "->", list(data.get("data", {}).keys()))
        except UpstoxApiError as exc:
            print("FAIL", key, "->", exc)


if __name__ == "__main__":
    asyncio.run(main())

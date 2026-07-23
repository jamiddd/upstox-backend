# Upstox Personal Backend

Personal FastAPI backend for Android/iPhone clients that integrates with Upstox.

## Goals
- Expose protected API endpoints for mobile clients
- Handle Upstox OAuth and encrypted token persistence
- Fetch REST market quote snapshots, holdings, and positions
- Run in Docker on a VPS
- Persist short-lived five-minute OI snapshots in an embedded SQLite database

## Project structure
- app/: FastAPI application code
- tests/: automated tests
- Dockerfile and docker-compose.yml: container setup

## OI snapshot retention

For every underlying selected through the tracked-instruments setting, the backend stores one
lossless OI analysis snapshot per wall-clock-aligned five-minute NSE market slot (`09:15` through
`15:25` IST). Summary values and per-strike OI/change-OI are also normalized into SQLite for fast
analysis. The database defaults to `/data/oi_snapshots.sqlite3`, which is covered by the existing
Docker volume.

The same database stores the five-minute ATR, VWAP/level distance, PCR, support/resistance OI, and
ATM-straddle history used by underlying-signal deltas. This makes those deltas restart-safe and
allows authenticated clients to retrieve the history later.

`GET /api/main/oi-snapshots/history` provides a lightweight summary-only listing of retained OI
slots for clients that need to choose time points without downloading raw or per-strike data.
`GET /api/main/oi-snapshots/diff` compares aggregate and per-strike current OI between two exact
slots selected from that listing.

Expiry-day OI and signal data remains available through the full session. Shortly after midnight
IST, snapshots with an earlier expiry date are deleted; startup performs the same cleanup if the
service was offline overnight. Set `OI_DATABASE_PATH` to override the database location.

## Development
Create a virtualenv and install dependencies:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Generate a Fernet encryption key for `TOKEN_ENCRYPTION_KEY`:

```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Run tests:

```bash
.venv/bin/python -m pytest
```

Run the app locally:

```bash
.venv/bin/uvicorn app.main:app --reload
```

Run the Docker smoke test after starting the container:

```bash
docker compose up --build -d
./scripts/smoke_test.sh
```

Validate Upstox OAuth/read-only endpoints after configuring credentials:

```bash
BASE_URL=http://localhost:8000 ./scripts/validate_readonly.sh
```

For the live deployment, use:

```bash
BASE_URL=https://api.scalp8.xyz MOBILE_API_KEY=<MOBILE_API_KEY> ./scripts/validate_readonly.sh
```

Protected endpoints require:

```text
X-API-Key: <MOBILE_API_KEY>
```

## V1 endpoints
- `GET /health`
- `GET /api/status`
- `GET /api/auth/login-url`
- `GET /api/auth/callback?code=...`
- `GET /api/auth/status`
- `POST /api/auth/logout`
- `GET /api/market/feed/authorize`
- `GET /api/market/ltp?instrument_key=...`
- `GET /api/market/quotes?instrument_key=...`
- `GET /api/market/candles?instrument_key=...&unit=minutes&interval=5&from_date=...&to_date=...`
- `GET /api/charges/brokerage?instrument_key=...&quantity=...&product=...&transaction_type=...&price=...`
- `GET /api/portfolio/holdings`
- `GET /api/portfolio/positions`
- `GET /api/user/get-funds-and-margin`

## Deployment
Production API base URL:

```text
https://api.scalp8.xyz
```

See `docs/VPS_DEPLOYMENT.md` for the VPS checklist, smoke test steps, OAuth validation, and reverse proxy notes.

## Screen APIs
- Main screen backend contract: `docs/MAIN_SCREEN_API.md`
- Search screen backend contract: `docs/SEARCH_SCREEN_API.md`
- Order history screen backend contract: `docs/ORDER_HISTORY_SCREEN_API.md`
- Order placement backend contract: `docs/ORDER_PLACEMENT_API.md`
- Brokerage estimation backend contract: `docs/BROKERAGE_API.md`
- Price chart candle contract: `docs/CHART_API.md`

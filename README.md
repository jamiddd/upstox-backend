# Upstox Personal Backend

Personal FastAPI backend for Android/iPhone clients that integrates with Upstox.

## Goals
- Expose protected API endpoints for mobile clients
- Handle Upstox OAuth and encrypted token persistence
- Fetch REST market quote snapshots, holdings, and positions
- Run in Docker on a VPS
- Keep the first version database-free

## Project structure
- app/: FastAPI application code
- tests/: automated tests
- Dockerfile and docker-compose.yml: container setup

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
- `GET /api/market/ltp?instrument_key=...`
- `GET /api/market/quotes?instrument_key=...`
- `GET /api/portfolio/holdings`
- `GET /api/portfolio/positions`

## Deployment
See `docs/VPS_DEPLOYMENT.md` for the VPS checklist, smoke test steps, and reverse proxy notes.

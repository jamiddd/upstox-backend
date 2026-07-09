# Upstox Scalper Backend

This repository will host a backend API for an Android app that integrates with Upstox.

## Goals
- Expose a small set of API endpoints for the mobile app
- Use the Upstox Python SDK
- Run in Docker on a VPS
- Keep the implementation simple and beginner-friendly

## Project structure
- app/: FastAPI application code
- tests/: automated tests
- docker/: container-related files

## Development
Run the app locally with:

```bash
uvicorn app.main:app --reload
```

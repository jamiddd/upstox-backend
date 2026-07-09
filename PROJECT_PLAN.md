# Upstox Scalper Backend API Plan

## 1. Goal
Build a backend API service that uses the Upstox Python SDK and exposes endpoints for an Android app to authenticate, fetch market data, and place trades. There will be no database and no persistent storage layer.

## 2. Recommended stack
- Language: Python 3.12
- Framework: FastAPI
- Upstox SDK: official Python SDK
- HTTP client: httpx or requests
- Authentication: OAuth flow handled by the backend
- Containerization: Docker + Docker Compose
- Hosting: VPS running the backend container
- Version control: GitHub

## 3. Core constraints
- No database access
- No persistent storage requirement
- The backend should be stateless where possible
- Tokens and session state may be held in memory or in secure server-side storage if needed later
- The Android app will call these API endpoints

## 4. Initial scope
Phase 1 should focus on the API foundation:
- health endpoint
- Upstox OAuth callback and token exchange
- token refresh handling
- market quotes endpoint
- order placement endpoint
- portfolio and positions endpoint
- paper trading mode first

## 5. Suggested project structure
- app/
  - main.py
  - api/
  - core/
  - services/
  - schemas/
  - config/
- tests/
- docker/
- Dockerfile
- docker-compose.yml
- .env.example
- requirements.txt or pyproject.toml
- README.md

## 6. Core modules
- Auth service
  - Upstox OAuth login initiation
  - callback handling
  - token refresh logic
- Market data service
  - fetch quotes and instrument data
  - expose simple REST endpoints for the mobile app
- Order service
  - place, modify, and cancel orders
  - enforce simple risk limits in code
- Session handling
  - keep user session context per authenticated client
- API contracts
  - define clear request and response schemas for Android integration

## 7. Development workflow
1. Create GitHub repository
2. Initialize FastAPI project structure
3. Add Docker setup for the backend
4. Implement health and auth endpoints
5. Add market data and order endpoints
6. Add environment-based config and deployment setup
7. Deploy to VPS and validate

## 8. GitHub and repo workflow
- Create one private repository for the backend
- Use main as the default branch
- Use feature branches for each task
- Add GitHub Actions for linting and tests
- Keep secrets in environment variables and server config

## 9. Docker and deployment plan
- Build a backend container image
- Run it with Docker Compose on the VPS
- Expose only the required HTTP port
- Use environment variables for Upstox credentials and app config
- Optionally place behind Nginx or a reverse proxy later

## 10. Security notes
- Do not commit secrets or tokens
- Keep credentials in environment variables
- Use HTTPS in deployment
- Prefer short-lived tokens and refresh handling
- If needed later, add secure server-side storage for tokens, but keep the initial version simple

## 11. Coding standards
- Every function must have documentation strings.
- Add comments generously for non-obvious logic and for calls to third-party libraries such as the Upstox SDK.
- Prefer clear, beginner-friendly code structure over clever shortcuts.
- Keep comments focused on explaining intent, flow, and integration points.

## 12. Recommended first milestones
- Milestone 1: FastAPI app skeleton + Docker + health endpoint
- Milestone 2: Upstox OAuth flow and token handling
- Milestone 3: market data endpoint
- Milestone 4: order placement endpoint
- Milestone 5: VPS deployment and smoke test

## 12. Suggested next step
I will scaffold the backend API structure, add the Docker setup, and start with the health endpoint and Upstox auth integration.

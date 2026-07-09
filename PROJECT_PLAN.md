# Upstox Personal Backend V1 Plan

## 1. Goal
Build a personal backend API service that uses Upstox REST APIs and exposes endpoints for Android/iPhone apps to authenticate, fetch market data, and view portfolio data. There will be no database in v1; the Upstox token is stored in an encrypted file on the VPS.

## 2. Recommended stack
- Language: Python 3.12
- Framework: FastAPI
- Upstox API: REST calls via httpx for v1
- Authentication: OAuth flow handled by the backend; mobile app protected by `X-API-Key`
- Containerization: Docker + Docker Compose
- Hosting: VPS running the backend container
- Version control: GitHub

## 3. Core constraints
- No database access
- No database requirement
- The backend should be stateless where possible
- Upstox token is stored in an encrypted server-side file
- Android/iPhone apps will call these API endpoints over HTTPS

## 4. Initial scope
Phase 1 should focus on the API foundation:
- health endpoint
- Upstox OAuth login URL, callback, token exchange, status, and logout
- encrypted token persistence
- mobile API key protection
- market quotes endpoint
- portfolio holdings and positions endpoint
- no live order placement in v1

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
- Backend auth
  - static `X-API-Key` protection for `/api/*`
- Upstox auth service
  - Upstox OAuth login initiation
  - callback handling
  - encrypted token save/load/delete
- Market data service
  - fetch LTP and full quote snapshots
  - expose simple REST endpoints for the mobile app
- Portfolio service
  - fetch holdings and positions
- Error handling
  - normalize Upstox errors into simple JSON responses

## 7. Development workflow
1. Create GitHub repository
2. Initialize FastAPI project structure
3. Add Docker setup for the backend
4. Implement health, mobile API key auth, and Upstox auth endpoints
5. Add market data and portfolio endpoints
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
- Mount a Docker volume at `/data` for encrypted token persistence
- Expose only the required HTTP port
- Use environment variables for Upstox credentials and app config
- Place behind HTTPS using Nginx/Caddy/reverse proxy before mobile use

## 10. Security notes
- Do not commit secrets or tokens
- Keep credentials in environment variables
- Use HTTPS in deployment
- Protect `/api/*` routes with `X-API-Key`
- Store Upstox tokens only in encrypted form
- Recheck Upstox rate limits and regulatory constraints before adding order placement

## 11. Coding standards
- Every function must have documentation strings.
- Add comments generously for non-obvious logic and for calls to third-party libraries such as the Upstox SDK.
- Prefer clear, beginner-friendly code structure over clever shortcuts.
- Keep comments focused on explaining intent, flow, and integration points.

## 12. Recommended first milestones
- Milestone 1: FastAPI app skeleton + Docker + health endpoint
- Milestone 2: mobile API key auth + Upstox OAuth + encrypted token handling
- Milestone 3: market data and portfolio read-only endpoints
- Milestone 4: paper/sandbox order placement endpoint
- Milestone 5: VPS deployment and smoke test

## 12. Suggested next step
Implement V1 read-only backend foundation, then validate locally and in Docker before adding any paper/sandbox order workflow.

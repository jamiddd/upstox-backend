# VPS Deployment Checklist

Production API base URL:

```text
https://api.scalp8.xyz
```

Production Upstox callback URL:

```text
https://api.scalp8.xyz/api/auth/callback
```

## 1. Prepare Secrets
- Create an Upstox developer app.
- Register the final callback URL. Production uses `https://api.scalp8.xyz/api/auth/callback`.
- Fill `.env` on the VPS:
  - `UPSTOX_API_KEY`
  - `UPSTOX_API_SECRET`
  - `UPSTOX_REDIRECT_URL=https://api.scalp8.xyz/api/auth/callback`
  - `MOBILE_API_KEY`
  - `TOKEN_ENCRYPTION_KEY`
  - `TOKEN_STORE_PATH=/data/upstox_token.enc`

Generate keys:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 2. Start The Container
```bash
docker compose up --build -d
docker compose ps
```

## 3. Run Smoke Test
```bash
./scripts/smoke_test.sh
```

Expected checks:
- `/health` returns `{"status":"ok"}`.
- `/api/status` returns `401` without `X-API-Key`.
- `/api/status` returns `{"status":"ready"}` with `X-API-Key`.

## 4. Test Upstox Login
```bash
curl -H "X-API-Key: $MOBILE_API_KEY" \
  https://api.scalp8.xyz/api/auth/login-url
```

Open the returned `login_url`, complete Upstox login, and confirm the browser callback returns:

```json
{"status":"authenticated"}
```

Then check:

```bash
curl -H "X-API-Key: $MOBILE_API_KEY" \
  https://api.scalp8.xyz/api/auth/status
```

Expected response:

```json
{"authenticated":true}
```

The OAuth callback itself is intentionally public because Upstox redirects the browser to `/api/auth/callback` without custom headers.

## 5. Test Read-Only Endpoints
You can run the full read-only validation script:

```bash
BASE_URL=https://api.scalp8.xyz MOBILE_API_KEY=$MOBILE_API_KEY ./scripts/validate_readonly.sh
```

Or call endpoints manually:

```bash
curl -H "X-API-Key: $MOBILE_API_KEY" \
  "https://api.scalp8.xyz/api/market/ltp?instrument_key=NSE_EQ%7CINE848E01016"

curl -H "X-API-Key: $MOBILE_API_KEY" \
  https://api.scalp8.xyz/api/portfolio/holdings
```

## 6. Reverse Proxy
Use HTTPS before mobile app use. Caddy is the simplest option:

```caddyfile
api.scalp8.xyz {
    reverse_proxy 127.0.0.1:8000
}
```

After HTTPS is active, `UPSTOX_REDIRECT_URL` and the Upstox developer app callback URL must both be `https://api.scalp8.xyz/api/auth/callback`.

Validate HTTPS through Caddy:

```bash
curl https://api.scalp8.xyz/health
```

Expected response:

```json
{"status":"ok"}
```

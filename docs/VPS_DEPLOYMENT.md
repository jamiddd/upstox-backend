# VPS Deployment Checklist

## 1. Prepare Secrets
- Create an Upstox developer app.
- Register the final callback URL, for example `https://api.yourdomain.com/api/auth/callback`.
- Fill `.env` on the VPS:
  - `UPSTOX_API_KEY`
  - `UPSTOX_API_SECRET`
  - `UPSTOX_REDIRECT_URL`
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
  https://api.yourdomain.com/api/auth/login-url
```

Open the returned `login_url`, complete Upstox login, then check:

```bash
curl -H "X-API-Key: $MOBILE_API_KEY" \
  https://api.yourdomain.com/api/auth/status
```

## 5. Test Read-Only Endpoints
```bash
curl -H "X-API-Key: $MOBILE_API_KEY" \
  "https://api.yourdomain.com/api/market/ltp?instrument_key=NSE_EQ%7CINE848E01016"

curl -H "X-API-Key: $MOBILE_API_KEY" \
  https://api.yourdomain.com/api/portfolio/holdings
```

## 6. Reverse Proxy
Use HTTPS before mobile app use. Caddy is the simplest option:

```caddyfile
api.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
```

After HTTPS is active, update `UPSTOX_REDIRECT_URL` and the Upstox developer app callback URL to the same HTTPS value.

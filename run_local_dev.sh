#!/usr/bin/env bash
# Run the backend locally for frontend dev, WITHOUT touching real Upstox credentials.
#
# Safety (see PersonalScalperWeb memory "feedback-local-backend-safety"): .env holds real
# UPSTOX_TOTP_* secrets. python-dotenv's load_dotenv() never overrides a var already present
# in the process environment, so we explicitly export empty TOTP vars here -- this guarantees
# auto_login_scheduler's startup login check fails fast (require_upstox_totp_login) instead of
# firing a real login attempt against the live account.
#
# All *_PATH settings are redirected under ./.localdata (git-ignored) since /data doesn't exist
# outside the production container.
#
# WEB_CLIENT_ORIGIN is set to the local SvelteKit dev server origin so CORS allows it; adjust the
# port if `npm run dev` picks a different one. WEB_SESSION_SECRET is a throwaway local-only value
# (not the production secret) -- fine since this issues/verifies its own local session cookies only.

set -euo pipefail
cd "$(dirname "$0")"

export UPSTOX_TOTP_USERNAME=
export UPSTOX_TOTP_SECRET=
export UPSTOX_TOTP_PIN=

export TOKEN_STORE_PATH="$(pwd)/.localdata/upstox_token.enc"
export TRACKED_INSTRUMENTS_PATH="$(pwd)/.localdata/tracked_instruments.json"
export WATCHLIST_PATH="$(pwd)/.localdata/watchlist.json"
export ACCOUNT_SNAPSHOT_PATH="$(pwd)/.localdata/account_snapshot.json"
export AUTO_LOGIN_STATE_PATH="$(pwd)/.localdata/auto_login_state.json"
export OI_DATABASE_PATH="$(pwd)/.localdata/oi_snapshots.sqlite3"
export ATM_IV_DATABASE_PATH="$(pwd)/.localdata/atm_iv_snapshots.sqlite3"
export NOTIFICATION_DATABASE_PATH="$(pwd)/.localdata/notifications.sqlite3"
export JOURNAL_DATABASE_PATH="$(pwd)/.localdata/journal.sqlite3"
export ORDER_ENGINE_LEDGER_DATABASE_PATH="$(pwd)/.localdata/order_engine_ledger.sqlite3"
export GTT_DATABASE_PATH="$(pwd)/.localdata/gtt_history.sqlite3"
export DEVICE_TOKEN_PATH="$(pwd)/.localdata/device_token.json"
export MAX_LOSS_SETTINGS_PATH="$(pwd)/.localdata/max_loss_settings.json"
export LOG_FILE_PATH="$(pwd)/.localdata/app.log"

export WEB_CLIENT_ORIGIN="${WEB_CLIENT_ORIGIN:-http://localhost:5173}"
export WEB_SESSION_SECRET="${WEB_SESSION_SECRET:-local-dev-only-not-a-real-secret}"
# This server has no TLS, so the session cookie can't carry Secure -- browsers silently refuse to
# store it otherwise, which is why login previously appeared to succeed (200) but every following
# request looked unauthenticated.
export WEB_SESSION_COOKIE_SECURE=false

mkdir -p .localdata

echo "Starting local backend on http://localhost:8000"
echo "  WEB_CLIENT_ORIGIN=$WEB_CLIENT_ORIGIN"
echo "  TOTP auto-login: disabled (blank credentials)"
echo "  Data dir: $(pwd)/.localdata"

if [ -x ".venv/bin/uvicorn" ]; then
  exec .venv/bin/uvicorn app.main:app --reload --port 8000
else
  exec uvicorn app.main:app --reload --port 8000
fi

#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
API_KEY="${MOBILE_API_KEY:-}"
INSTRUMENT_KEY="${INSTRUMENT_KEY:-NSE_EQ|INE848E01016}"

if [ -z "$API_KEY" ] && [ -f ".env" ]; then
  API_KEY="$(grep '^MOBILE_API_KEY=' .env | cut -d '=' -f 2-)"
fi

if [ -z "$API_KEY" ]; then
  echo "MOBILE_API_KEY is required in the environment or .env file."
  exit 1
fi

request() {
  local path="$1"
  local output="$2"
  local status_code

  status_code="$(curl -sS -G -o "$output" -w '%{http_code}' \
    -H "X-API-Key: $API_KEY" \
    "$BASE_URL$path")"

  if [ "$status_code" != "200" ]; then
    echo "Request failed: $path returned HTTP $status_code"
    cat "$output"
    echo
    exit 1
  fi
}

echo "Checking API status..."
request "/api/status" "/tmp/upstox_status.json"

echo "Fetching Upstox login URL..."
request "/api/auth/login-url" "/tmp/upstox_login_url.json"
cat "/tmp/upstox_login_url.json"
echo

echo "Checking Upstox auth status..."
request "/api/auth/status" "/tmp/upstox_auth_status.json"
cat "/tmp/upstox_auth_status.json"
echo

if grep -q '"authenticated":false' "/tmp/upstox_auth_status.json"; then
  echo "Upstox is not authenticated yet."
  echo "Open the login_url above, complete Upstox login, then run this script again."
  exit 0
fi

encoded_key="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$INSTRUMENT_KEY")"

echo "Checking LTP endpoint for $INSTRUMENT_KEY..."
request "/api/market/ltp?instrument_key=$encoded_key" "/tmp/upstox_ltp.json"

echo "Checking full quotes endpoint for $INSTRUMENT_KEY..."
request "/api/market/quotes?instrument_key=$encoded_key" "/tmp/upstox_quotes.json"

echo "Checking holdings endpoint..."
request "/api/portfolio/holdings" "/tmp/upstox_holdings.json"

echo "Checking positions endpoint..."
request "/api/portfolio/positions" "/tmp/upstox_positions.json"

echo "Read-only Upstox validation passed for $BASE_URL"

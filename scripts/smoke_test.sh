#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
API_KEY="${MOBILE_API_KEY:-}"

if [ -z "$API_KEY" ] && [ -f ".env" ]; then
  API_KEY="$(grep '^MOBILE_API_KEY=' .env | cut -d '=' -f 2-)"
fi

if [ -z "$API_KEY" ]; then
  echo "MOBILE_API_KEY is required in the environment or .env file."
  exit 1
fi

echo "Checking public health endpoint..."
health_response="$(curl -fsS "$BASE_URL/health")"
if [ "$health_response" != '{"status":"ok"}' ]; then
  echo "Unexpected /health response: $health_response"
  exit 1
fi

echo "Checking protected endpoint rejects missing API key..."
status_code="$(curl -sS -o /tmp/upstox_api_status_without_key.json -w '%{http_code}' "$BASE_URL/api/status")"
if [ "$status_code" != "401" ]; then
  echo "Expected 401 without API key, got $status_code"
  cat /tmp/upstox_api_status_without_key.json
  exit 1
fi

echo "Checking protected endpoint accepts API key..."
api_response="$(curl -fsS -H "X-API-Key: $API_KEY" "$BASE_URL/api/status")"
if [ "$api_response" != '{"status":"ready"}' ]; then
  echo "Unexpected /api/status response: $api_response"
  exit 1
fi

echo "Smoke test passed for $BASE_URL"

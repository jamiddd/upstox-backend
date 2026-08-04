#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/upstox-api"

cd "$APP_DIR"

echo "Pulling latest changes..."
git pull

echo "Rebuilding and restarting containers..."
docker compose up --build -d

echo "Current container status:"
docker compose ps

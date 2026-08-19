#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-28081}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker first, then run this script again."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is required."
  exit 1
fi

sed -i -E "s/[0-9]+:8080/${PORT}:8080/" docker-compose.yml
mkdir -p data/uploads

echo "Building Stream247 Panel..."
docker compose up -d --build

echo
echo "Done. Open: http://YOUR_VPS_IP:${PORT}"
echo "Logs: docker compose logs -f"

#!/usr/bin/env bash
# Phase 7 — build the actual deploy artifact and run it (not `uvicorn` on the
# host) — this is the one earlier phases don't cover, and the closest local
# proxy for the server deploy itself.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
cd "$PROJECT_ROOT"

echo "=== docker build ==="
docker build -t sadi:local-test .

echo "=== docker compose up -d --build (full stack incl. the sadi container) ==="
docker compose up -d --build

echo "Waiting for the sadi container to come up..."
sleep 5
curl -s "$BASE_URL/v1/health" | jq

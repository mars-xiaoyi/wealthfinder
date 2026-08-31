#!/usr/bin/env bash
# Phase 1 — health check against the running service.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

echo "=== GET /v1/health ==="
curl -s "$BASE_URL/v1/health" | jq

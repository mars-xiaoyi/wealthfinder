#!/usr/bin/env bash
# Phase 6 — dependency-failure paths: stop redis/postgres in turn and confirm
# /v1/health degrades correctly and /v1/crawl returns COMMON-5001.
# Disruptive but self-healing — restarts each dependency before moving on.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

echo "=== Stopping redis ==="
compose stop redis
echo "--- GET /v1/health (expect HTTP 503, redis=error) ---"
curl -s -D /dev/stderr "$BASE_URL/v1/health" | jq
echo "--- POST /v1/crawl (expect COMMON-5001) ---"
curl -s -X POST "$BASE_URL/v1/crawl" -H 'Content-Type: application/json' \
  -d "{\"execution_id\":\"$(uuidgen)\",\"source_name\":\"YAHOO_HK\"}" | jq
echo "=== Restarting redis ==="
compose start redis
sleep 3

echo
echo "=== Stopping postgres ==="
compose stop postgres
echo "--- GET /v1/health (expect HTTP 503, database=error) ---"
curl -s -D /dev/stderr "$BASE_URL/v1/health" | jq
echo "=== Restarting postgres ==="
compose start postgres
sleep 3

echo "=== Final check — both back up ==="
curl -s "$BASE_URL/v1/health" | jq

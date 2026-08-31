#!/usr/bin/env bash
# Phase 4 — cleaned_news retrieval API: single fetch, 404, batch fetch,
# batch validation error. Run after 02/03 so at least one cleaned_news row exists.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

CID=$(psql_sadi -tA -c "SELECT cleaned_id FROM cleaned_news LIMIT 1;")
if [ -z "$CID" ]; then
  echo "No cleaned_news rows yet — run 02-crawl.sh and 03-cleaning.sh first." >&2
  exit 1
fi

echo "=== GET /v1/cleaned_news/$CID ==="
curl -s "$BASE_URL/v1/cleaned_news/$CID" | jq

echo "=== GET /v1/cleaned_news/<random> (expect 404 COMMON-4004) ==="
curl -s "$BASE_URL/v1/cleaned_news/$(uuidgen)" | jq

echo "=== POST /v1/cleaned_news/batch (found) ==="
curl -s -X POST "$BASE_URL/v1/cleaned_news/batch" -H 'Content-Type: application/json' \
  -d "{\"cleaned_ids\":[\"$CID\"]}" | jq

echo "=== POST /v1/cleaned_news/batch (empty list, expect COMMON-4001) ==="
curl -s -X POST "$BASE_URL/v1/cleaned_news/batch" -H 'Content-Type: application/json' \
  -d '{"cleaned_ids":[]}' | jq

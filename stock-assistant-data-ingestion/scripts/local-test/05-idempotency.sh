#!/usr/bin/env bash
# Phase 5 — dedup/idempotency: re-crawl the same source and confirm raw_news
# row count doesn't grow (raw_hash/source_url UNIQUE no-ops the insert).
#
# Usage: ./05-idempotency.sh [SOURCE_NAME]   (default: YAHOO_HK)
#
# Note: for RSS-based sources (YAHOO_HK/AASTOCKS/MINGPAO) a genuinely new
# article published between the two runs will also change the count — that's
# not a failure, just re-run closer together or check crawl_error_log/timing.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SRC="${1:-YAHOO_HK}"

BEFORE=$(psql_sadi -tA -c "SELECT count(*) FROM raw_news WHERE source_name='$SRC';")
echo "raw_news count for $SRC before re-crawl: $BEFORE"

EID=$(uuidgen)
curl -s -X POST "$BASE_URL/v1/crawl" -H 'Content-Type: application/json' \
  -d "{\"execution_id\":\"$EID\",\"source_name\":\"$SRC\"}" | jq

echo "Waiting 10s for the crawl to finish..."
sleep 10

AFTER=$(psql_sadi -tA -c "SELECT count(*) FROM raw_news WHERE source_name='$SRC';")
echo "raw_news count for $SRC after re-crawl:  $AFTER"

if [ "$BEFORE" = "$AFTER" ]; then
  echo "PASS — dedup held, no new rows inserted"
else
  echo "Row count changed ($BEFORE -> $AFTER) — check whether a new article was genuinely published, or this is a dedup regression"
fi

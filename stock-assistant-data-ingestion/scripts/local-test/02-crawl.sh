#!/usr/bin/env bash
# Phase 2 — trigger a crawl for all 4 sources, verify persistence + completion
# signal, then check the two documented validation-error paths.
#
# Override the HKEX date if you want a known-nonzero-volume day instead of
# today, e.g.: HKEX_DATE=2026-04-14 ./02-crawl.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

HKEX_DATE="${HKEX_DATE:-$(date +%F)}"

for SRC in HKEX MINGPAO AASTOCKS YAHOO_HK; do
  EID=$(uuidgen)
  DATE_FIELD=""
  if [ "$SRC" = "HKEX" ]; then
    DATE_FIELD=",\"date\":\"$HKEX_DATE\""
  fi
  echo "=== $SRC (execution_id=$EID) ==="
  curl -s -X POST "$BASE_URL/v1/crawl" \
    -H 'Content-Type: application/json' \
    -d "{\"execution_id\":\"$EID\",\"source_name\":\"$SRC\"$DATE_FIELD}" | jq
  echo
done

echo "Waiting 15s for crawls to finish before checking DB/Redis..."
sleep 15

echo "=== raw_news rows by source ==="
psql_sadi -c "SELECT source_name, count(*) FROM raw_news GROUP BY source_name;"

echo "=== crawl_error_log by source/error_code ==="
psql_sadi -c "SELECT source_name, error_code, count(*) FROM crawl_error_log GROUP BY 1,2;"

echo "=== stream:crawl_completed (last 10) ==="
redis_sadi XRANGE stream:crawl_completed - + COUNT 10

echo
echo "=== Error-path checks ==="
echo "--- COMMON-4001: unknown source_name ---"
curl -s -X POST "$BASE_URL/v1/crawl" -H 'Content-Type: application/json' \
  -d "{\"execution_id\":\"$(uuidgen)\",\"source_name\":\"NOT_A_SOURCE\"}" | jq

echo "--- COMMON-4000: malformed JSON ---"
curl -s -X POST "$BASE_URL/v1/crawl" -H 'Content-Type: application/json' -d '{bad json' | jq

#!/usr/bin/env bash
# Phase 3 — verify the cleaning pipeline: raw_news survival breakdown,
# cleaned_news population, and the stream signal to downstream consumers.
# Run after 02-crawl.sh.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

echo "=== raw_news survival breakdown ==="
psql_sadi -c "SELECT count(*) AS raw, count(*) FILTER (WHERE NOT is_deleted) AS survived,
    count(*) FILTER (WHERE deleted_reason='BODY_TOO_SHORT') AS too_short,
    count(*) FILTER (WHERE deleted_reason='EMPTY_FIELD') AS empty
  FROM raw_news;"

echo "=== cleaned_news count ==="
psql_sadi -c "SELECT count(*) FROM cleaned_news;"

echo "=== stream:raw_news_cleaned (last 10) ==="
redis_sadi XRANGE stream:raw_news_cleaned - + COUNT 10

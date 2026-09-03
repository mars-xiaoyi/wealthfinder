#!/bin/bash
# Daily safety-net for the unattended volume-test run. Not part of the final
# metric collection (that's the SQL query in volume-test-plan.md, run once
# at the end) — this just catches a broken crawl early rather than on day 14.
#
# 1. Confirms today's 4 crawls actually reported completion on
#    stream:crawl_completed (not just the HTTP 202 accepted at trigger time).
# 2. Logs today's row-count snapshot so a stall is visible in the log trend.

set -euo pipefail

# Cron's environment has no CWD context, so both are made explicit here.
REPO_DIR="/opt/sadi"
COMPOSE="docker compose -f $REPO_DIR/docker-compose-dev.yml"
cd "$REPO_DIR"

LOGDIR="/opt/sadi/logs"
mkdir -p "$LOGDIR"
TODAY="$(date +%F)"
HEALTHLOG="$LOGDIR/healthcheck_${TODAY}.log"

echo "=== $(date -u +%FT%TZ) daily healthcheck ===" | tee -a "$HEALTHLOG"

# 1. Check stream:crawl_completed for today's entries (Redis Streams XRANGE).
#    Just prints raw entries for eyeballing — not parsed, since execution_id
#    correlation isn't needed here (Admin's scheduler doesn't exist yet, so
#    there's no second system to correlate against).
echo "--- stream:crawl_completed entries (last 20) ---" | tee -a "$HEALTHLOG"
$COMPOSE exec -T redis redis-cli XREVRANGE stream:crawl_completed + - COUNT 20 \
  >> "$HEALTHLOG" 2>&1 || echo "WARNING: could not read stream:crawl_completed" | tee -a "$HEALTHLOG"

# 2. Row-count snapshot (cheap trend line — a flat count day-over-day means
#    something stopped ingesting).
echo "--- raw_news / cleaned_news counts ---" | tee -a "$HEALTHLOG"
$COMPOSE exec -T postgres psql -U postgres -d sadi -c "
  SELECT r.source_name,
         count(*) FILTER (WHERE date(r.created_at) = current_date) AS raw_today,
         count(*) AS raw_total
  FROM raw_news r
  GROUP BY 1
  ORDER BY 1;
" >> "$HEALTHLOG" 2>&1 || echo "WARNING: could not query postgres" | tee -a "$HEALTHLOG"

echo "=== end healthcheck ===" | tee -a "$HEALTHLOG"
